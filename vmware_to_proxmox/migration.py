"""Migration orchestrator — ties all ten steps together."""

import logging
import time

from .config import AppConfig
from .exceptions import GuestOperationError, MigrationError, ProxmoxOperationError
from .guest_ops import GuestOperations
from .proxmox import ProxmoxClient
from .vcenter import VCenterClient

logger = logging.getLogger(__name__)

TOTAL_STEPS = 10


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
        if self.config.migration.proxmox_final_storage:
            logger.info("  Proxmox final store: %s", self.config.migration.proxmox_final_storage)
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
            (8, "Start VM in Proxmox", self._step_8_start_vm),
            (9, "Move disks to final storage", self._step_9_move_disks),
            (10, "Verify VM on final storage", self._step_10_verify),
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
        vm_config = self._resolve_vm_config(vm)
        vmid = self._resolve_vmid()

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

    def _step_8_start_vm(self, vm):
        vmid = self._resolve_vmid()
        start_before = self.config.migration.start_vm_before_move

        if not start_before:
            logger.info("  start_vm_before_move=false — VM will start after disks are moved.")
            return

        if self.dry_run:
            logger.info("  DRY RUN: would start VMID %d", vmid)
            return

        self.px.start_vm(vmid)
        logger.info("  VM %d start command sent. Waiting 20s for VM to start ...", vmid)
        time.sleep(20)
        logger.info("  Ready to proceed.")

    def _step_9_move_disks(self, vm):
        vmid = self._resolve_vmid()
        vm_config = self._resolve_vm_config(vm)
        final_storage = self.config.migration.proxmox_final_storage
        start_before = self.config.migration.start_vm_before_move

        if not final_storage:
            raise MigrationError(
                "Cannot move disks: proxmox_final_storage is not set. "
                "Set --proxmox-final-storage or proxmox_final_storage in config."
            )

        # Build list of all disks to move
        disks_to_move = [f"scsi{i}" for i in range(len(vm_config["disks"]))]
        if vm_config["firmware"] == "efi":
            disks_to_move.append("efidisk0")

        total = len(disks_to_move)
        logger.info("  %d disk(s) to move.", total)

        # Move disks one at a time with progress
        for idx, disk_name in enumerate(disks_to_move, start=1):
            pct = int(idx / total * 100)
            if self.dry_run:
                logger.info("  DRY RUN: [%d/%d %3d%%] would move %s -> %s (qcow2)",
                            idx, total, pct, disk_name, final_storage)
                continue
            logger.info("  [%d/%d %3d%%] Moving %s ...", idx, total, pct, disk_name)
            self.px.move_disk(vmid, disk_name, final_storage)

        logger.info("  All disks moved to %s.", final_storage)

        # Start VM after move if not started in step 8
        if not start_before:
            if self.dry_run:
                logger.info("  DRY RUN: would start VMID %d after move", vmid)
                return
            logger.info("  Starting VM after disk move ...")
            self.px.start_vm(vmid)
            logger.info("  VM %d start command sent. Waiting 20s for VM to start ...", vmid)
            time.sleep(20)
            logger.info("  Ready to proceed.")

    def _step_10_verify(self, vm):
        vmid = self._resolve_vmid()
        vm_config = self._resolve_vm_config(vm)
        final_storage = self.config.migration.proxmox_final_storage

        if self.dry_run:
            logger.info("  DRY RUN: would verify VMID %d is running on %s", vmid, final_storage)
            return

        # Verify VM is running
        status = self.px.get_vm_status(vmid)
        logger.info("  VM %d status: %s", vmid, status)
        if status != "running":
            raise ProxmoxOperationError(
                f"VM {vmid} is not running (status: {status})"
            )

        # Verify all disks are on the final storage
        px_config = self.px.get_vm_config_proxmox(vmid)
        disk_keys = [f"scsi{i}" for i in range(len(vm_config["disks"]))]
        if vm_config["firmware"] == "efi":
            disk_keys.append("efidisk0")

        for key in disk_keys:
            value = px_config.get(key, "")
            if not value.startswith(f"{final_storage}:"):
                raise ProxmoxOperationError(
                    f"Disk {key} is not on final storage {final_storage}: {value}"
                )
            logger.info("  %s: %s", key, value)

        logger.info("  Verification passed — all disks on %s, VM running.", final_storage)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_vmid(self) -> int:
        """Return the VMID from step 2 or from config."""
        vmid = getattr(self, "_vmid", None)
        if vmid is None:
            vmid = self.config.migration.proxmox_vmid
            if not vmid:
                raise MigrationError(
                    "VMID unknown. Run from step 2, or set proxmox_vmid in config."
                )
        return vmid

    def _resolve_vm_config(self, vm):
        """Return the vCenter vm_config from step 2 or fetch it fresh."""
        vm_config = getattr(self, "_vm_config", None)
        if vm_config is None:
            vm_config = self.vc.get_vm_config(vm)
        return vm_config

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
        logger.info("  1. Run importNicConfig.ps1 to restore network configuration")
        logger.info("  2. Run purge-vmware-tools.ps1 -Force to remove VMware Tools")
        logger.info("  3. Reboot the VM")
