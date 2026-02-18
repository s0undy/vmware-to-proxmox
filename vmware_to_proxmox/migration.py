"""Migration orchestrator — ties all seven steps together."""

import logging

from .config import AppConfig
from .exceptions import GuestOperationError, MigrationError
from .guest_ops import GuestOperations
from .proxmox import ProxmoxClient
from .vcenter import VCenterClient

logger = logging.getLogger(__name__)

TOTAL_STEPS = 7


class MigrationOrchestrator:
    def __init__(self, config: AppConfig, skip_to: int = 1, dry_run: bool = False):
        self.config = config
        self.skip_to = skip_to
        self.dry_run = dry_run
        self.vc = VCenterClient(config.vcenter)
        self.px = ProxmoxClient(config.proxmox)
        self.guest_ops = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self):
        """Execute the full migration workflow."""
        logger.info("=" * 60)
        logger.info("VMware-to-Proxmox Migration")
        logger.info("  VM:                  %s", self.config.migration.vm_name)
        logger.info("  Migration datastore: %s", self.config.migration.migration_datastore)
        logger.info("  Proxmox storage:     %s", self.config.migration.proxmox_storage)
        if self.dry_run:
            logger.info("  *** DRY RUN — no changes will be made ***")
        if self.skip_to > 1:
            logger.info("  Resuming from step %d", self.skip_to)
        logger.info("=" * 60)

        self._connect()

        vm = self.vc.get_vm_by_name(self.config.migration.vm_name)
        logger.info("Found VM: %s  (power: %s)", vm.name, vm.runtime.powerState)

        steps = [
            (1, "Storage vMotion", self._step_1_storage_vmotion),
            (2, "Create Proxmox VM", self._step_2_create_proxmox_vm),
            (3, "Export NIC configuration", self._step_3_export_nic_config),
            (4, "Enable VirtIO SCSI boot driver", self._step_4_enable_vioscsi),
            (5, "Install VirtIO guest tools", self._step_5_install_virtio_tools),
            (6, "Shut down VM", self._step_6_shutdown),
            (7, "Rewrite VMDK descriptors", self._step_7_rewrite_vmdk_descriptors),
        ]

        for num, label, fn in steps:
            if self.skip_to > num:
                logger.info("")
                logger.info("SKIP step %d/%d: %s", num, TOTAL_STEPS, label)
                continue
            logger.info("")
            logger.info("STEP %d/%d: %s", num, TOTAL_STEPS, label)
            logger.info("-" * 40)
            fn(vm)

        self._print_next_steps()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        logger.info("Connecting to vCenter %s ...", self.config.vcenter.host)
        self.vc.connect()
        logger.info("  Connected.")

        logger.info("Connecting to Proxmox %s ...", self.config.proxmox.host)
        self.px.connect()
        logger.info("  Connected.")

        self.guest_ops = GuestOperations(self.vc, self.config.guest)

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    def _step_1_storage_vmotion(self, vm):
        ds = self.vc.get_datastore_by_name(self.config.migration.migration_datastore)
        logger.info("  Target datastore: %s", ds.name)
        logger.info("  Free space:       %.1f GB", ds.summary.freeSpace / (1024 ** 3))

        if self.vc.vm_is_on_datastore(vm, ds):
            logger.info("  VM is already on datastore '%s' — skipping vMotion.", ds.name)
            return

        if self.dry_run:
            logger.info("  DRY RUN: would relocate VM to %s", ds.name)
            return

        logger.info("  Relocating VM (this may take a while) ...")
        self.vc.storage_vmotion(vm, ds)
        logger.info("  Storage vMotion complete.")

    def _step_2_create_proxmox_vm(self, vm):
        vm_config = self.vc.get_vm_config(vm)
        self._vm_config = vm_config

        logger.info("  CPUs:     %d  (%d sockets x %d cores)",
                     vm_config["num_cpus"],
                     max(1, vm_config["num_cpus"] // vm_config["num_cores_per_socket"]),
                     vm_config["num_cores_per_socket"])
        logger.info("  Memory:   %d MB", vm_config["memory_mb"])
        logger.info("  Firmware: %s", vm_config["firmware"])
        for i, d in enumerate(vm_config["disks"]):
            logger.info("  Disk scsi%d: %.1f GB  (%s)", i, d["size_gb"], d["label"])
        for i, n in enumerate(vm_config["nics"]):
            logger.info("  NIC  net%d:  %s  MAC %s", i, n["label"], n["mac"])

        if self.dry_run:
            logger.info("  DRY RUN: would create Proxmox VM")
            return

        vmid = self.px.create_vm(vm_config, self.config.migration)
        self._vmid = vmid
        logger.info("  Proxmox VM created — VMID %d", vmid)

    def _step_3_export_nic_config(self, vm):
        self.guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        script = self.config.migration.export_nic_script
        if self.dry_run:
            logger.info("  DRY RUN: would run %s", script)
            return

        exit_code = self.guest_ops.run_powershell(vm, script)
        if exit_code != 0:
            raise GuestOperationError(
                f"exportNicConfig.ps1 exited with code {exit_code}"
            )
        logger.info("  NIC config exported to network.json inside guest.")

    def _step_4_enable_vioscsi(self, vm):
        self.guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        script = self.config.migration.vioscsi_script
        driver_path = self.config.migration.virtio_driver_path
        args = f'-DriverPath "{driver_path}"'

        if self.dry_run:
            logger.info("  DRY RUN: would run %s %s", script, args)
            return

        exit_code = self.guest_ops.run_powershell(vm, script, arguments=args,
                                                  timeout_seconds=900)
        if exit_code != 0:
            raise GuestOperationError(
                f"enable-vioscsi-to-load-on-boot.ps1 exited with code {exit_code}"
            )
        logger.info("  VirtIO SCSI driver configured for boot loading.")

    def _step_5_install_virtio_tools(self, vm):
        self.guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        exe = self.config.migration.virtio_tools_path
        args = "/install /quiet /norestart"

        if self.dry_run:
            logger.info("  DRY RUN: would run %s %s", exe, args)
            return

        exit_code = self.guest_ops.run_executable(vm, exe, arguments=args,
                                                  timeout_seconds=600)
        if exit_code != 0:
            raise GuestOperationError(
                f"virtio-win-guest-tools.exe exited with code {exit_code}"
            )
        logger.info("  VirtIO guest tools installed.")

    def _step_6_shutdown(self, vm):
        if self.dry_run:
            logger.info("  DRY RUN: would shut down %s", vm.name)
            return

        logger.info("  Sending shutdown signal ...")
        self.vc.shutdown_guest(vm)
        logger.info("  VM is powered off.")

    def _step_7_rewrite_vmdk_descriptors(self, vm):
        # When resuming from this step (skipping step 2), fetch config anew.
        vm_config = getattr(self, "_vm_config", None)
        vmid = getattr(self, "_vmid", None)

        if vm_config is None:
            vm_config = self.vc.get_vm_config(vm)

        if vmid is None:
            vmid = self.config.migration.proxmox_vmid
            if not vmid:
                raise MigrationError(
                    "Cannot rewrite VMDK descriptors: VMID unknown. "
                    "Run from step 2, or set proxmox_vmid in config."
                )

        if self.dry_run:
            for i, d in enumerate(vm_config["disks"]):
                logger.info("  DRY RUN: would rewrite scsi%d: %s -> vm-%d-disk-%d.vmdk",
                            i, d["filename"], vmid, i)
            return

        self.px.rewrite_vmdk_descriptors(
            vmid=vmid,
            vm_config=vm_config,
            storage_name=self.config.migration.proxmox_storage,
        )
        logger.info("  All VMDK descriptors rewritten.")

    # ------------------------------------------------------------------
    # Post-migration guidance
    # ------------------------------------------------------------------

    def _print_next_steps(self):
        logger.info("")
        logger.info("=" * 60)
        logger.info("AUTOMATED STEPS COMPLETE")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Remaining manual steps:")
        logger.info("  1. Boot the VM on Proxmox")
        logger.info("  2. Run importNicConfig.ps1 to restore network configuration")
        logger.info("  3. Run purge-vmware-tools.ps1 -Force to remove VMware Tools")
        logger.info("  4. Reboot the VM")
