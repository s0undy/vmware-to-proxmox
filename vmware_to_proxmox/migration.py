"""Migration orchestrator — ties all ten steps together."""

import logging
import time

from .config import AppConfig
from .exceptions import GuestOperationError, MigrationError, ProxmoxOperationError
from .guest_ops import GuestOperations
from .proxmox import ProxmoxClient
from .vcenter import VCenterClient

logger = logging.getLogger(__name__)

TOTAL_STEPS = 14


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

    def run(self) -> dict:
        """Execute the full migration workflow.

        Returns:
            Dict with vm_name, final_storage, elapsed_seconds, and ip_address.
        """
        start_time = time.monotonic()
        vm_name = self.config.migration.vm_name

        logger.info("=" * 60)
        logger.info("VMware-to-Proxmox Migration")
        logger.info("  VM:                  %s", vm_name)
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

        vm = self.vc.get_vm_by_name(vm_name)
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
            (11, "Install VirtIO drivers from ISO", self._step_11_install_virtio_drivers),
            (12, "Purge VMware Tools", self._step_12_purge_vmware_tools),
            (13, "Restore NIC configuration", self._step_13_import_nic_config),
            (14, "Enable NICs and final reboot", self._step_14_enable_nics),
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

        # Query guest agent for primary IP address
        ip_address = None
        if not self.dry_run:
            vmid = self._resolve_vmid()
            self._wait_for_vm_ready(vmid)
            logger.info("  Waiting for QEMU guest agent ...")
            try:
                self.px.wait_for_guest_agent(vmid)
                ip_address = self.px.get_guest_ip(vmid)
            except Exception:
                logger.warning("  Could not retrieve IP from guest agent.")

        elapsed = time.monotonic() - start_time
        minutes, seconds = divmod(int(elapsed), 60)

        self._print_next_steps()
        logger.info("Migration of %s completed in %dm %ds", vm_name, minutes, seconds)

        return {
            "vm_name": vm_name,
            "final_storage": self.config.migration.proxmox_final_storage or self.config.migration.proxmox_storage,
            "elapsed_seconds": int(elapsed),
            "ip_address": ip_address,
        }

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
            logger.info("  NIC  net%d:  %s", i, n["label"])

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
        logger.info("  Waiting 30s for clean shutdown before modifying Proxmox ...")
        time.sleep(30)

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
        logger.info("  VM %d start command sent. Waiting 30s for VM to start cleanly ...", vmid)
        time.sleep(30)
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
            logger.info("  VM %d start command sent. Waiting 30s for VM to start cleanly ...", vmid)
            time.sleep(30)
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

    def _step_11_install_virtio_drivers(self, vm):
        vmid = self._resolve_vmid()
        iso_storage = self.config.migration.virtio_iso_storage
        iso_filename = self.config.migration.virtio_iso_filename

        if self.dry_run:
            logger.info("  DRY RUN: would mount %s:iso/%s on VMID %d and install VirtIO drivers",
                        iso_storage, iso_filename, vmid)
            return

        # Wait for the VM to be running and Windows to start up
        self._wait_for_vm_ready(vmid)

        # Mount the VirtIO ISO
        self.px.mount_iso(vmid, iso_storage, iso_filename)
        logger.info("  Waiting 5s for ISO to become available ...")
        time.sleep(5)

        # Wait for guest agent
        logger.info("  Waiting for QEMU guest agent ...")
        self.px.wait_for_guest_agent(vmid)
        logger.info("  Guest agent is responding.")

        # Discover the drive letter of the mounted ISO
        logger.info("  Discovering ISO drive letter ...")
        result = self.px.guest_exec(
            vmid,
            command="powershell",
            arguments=["-Command",
                       "(Get-Volume | Where-Object {$_.FileSystemLabel -like 'virtio-win*'}).DriveLetter"],
            timeout=60,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"Failed to discover ISO drive letter: exit code {result['exitcode']}, "
                f"stderr: {result.get('err-data', '')}"
            )
        drive_letter = result.get("out-data", "").strip()
        if not drive_letter or len(drive_letter) != 1:
            raise ProxmoxOperationError(
                f"Unexpected drive letter result: '{drive_letter}'"
            )
        logger.info("  VirtIO ISO mounted on drive %s:", drive_letter)

        # Install the full VirtIO driver package via msiexec
        msi_path = f"{drive_letter}:\\virtio-win-gt-x64.msi"
        logger.info("  Installing VirtIO drivers from %s ...", msi_path)
        result = self.px.guest_exec(
            vmid,
            command="msiexec",
            arguments=["/i", msi_path, "/quiet", "/qn", "/norestart"],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"msiexec failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        logger.info("  VirtIO driver package installed.")

    def _step_12_purge_vmware_tools(self, vm):
        vmid = self._resolve_vmid()
        script = self.config.migration.purge_vmware_script

        if self.dry_run:
            logger.info("  DRY RUN: would run %s -Force on VMID %d", script, vmid)
            return

        # Wait for the VM to be running and Windows to start up
        self._wait_for_vm_ready(vmid)

        logger.info("  Waiting for QEMU guest agent ...")
        self.px.wait_for_guest_agent(vmid)
        logger.info("  Guest agent is responding.")

        logger.info("  Running purge-vmware-tools.ps1 -Force ...")
        result = self.px.guest_exec(
            vmid,
            command="powershell",
            arguments=["-ExecutionPolicy", "Bypass", "-File", script, "-Force"],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"purge-vmware-tools.ps1 failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        logger.info("  VMware Tools purged.")

        logger.info("  Waiting 10s before reboot ...")
        time.sleep(10)
        self.px.reboot_vm(vmid)
        logger.info("  Waiting 20s for VM to start up ...")
        time.sleep(20)

    def _step_13_import_nic_config(self, vm):
        vmid = self._resolve_vmid()
        script = self.config.migration.import_nic_script

        if self.dry_run:
            logger.info("  DRY RUN: would run %s on VMID %d", script, vmid)
            return

        # Wait for the VM to be running and Windows to start up
        self._wait_for_vm_ready(vmid)

        logger.info("  Waiting for QEMU guest agent ...")
        self.px.wait_for_guest_agent(vmid)
        logger.info("  Guest agent is responding.")

        logger.info("  Running importNicConfig.ps1 ...")
        result = self.px.guest_exec(
            vmid,
            command="powershell",
            arguments=["-ExecutionPolicy", "Bypass", "-File", script],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"importNicConfig.ps1 failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        logger.info("  NIC configuration restored.")

        logger.info("  Waiting 5s before reboot ...")
        time.sleep(5)
        self.px.reboot_vm(vmid)
        logger.info("  Waiting 20s for VM to start up ...")
        time.sleep(20)

    def _step_14_enable_nics(self, vm):
        vmid = self._resolve_vmid()

        if self.dry_run:
            logger.info("  DRY RUN: would unmount ISO, enable all NICs, and reboot VMID %d", vmid)
            return

        # Unmount the VirtIO ISO
        self.px.unmount_iso(vmid)

        # Enable all NICs (set link_down=0)
        px_config = self.px.get_vm_config_proxmox(vmid)
        nic_keys = sorted(k for k in px_config if k.startswith("net") and k[3:].isdigit())
        for nic_key in nic_keys:
            logger.info("  Enabling %s ...", nic_key)
            self.px.set_nic_link_state(vmid, nic_key, link_down=False)
        logger.info("  All NICs enabled.")

        # Final reboot
        self.px.reboot_vm(vmid)
        logger.info("  Final reboot initiated.")

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

    def _wait_for_vm_ready(self, vmid: int, settle_seconds: int = 30) -> None:
        """Wait until Proxmox reports the VM as running, then wait extra time for Windows to boot."""
        logger.info("  Waiting for VM %d to be running ...", vmid)
        for _ in range(60):
            status = self.px.get_vm_status(vmid)
            if status == "running":
                break
            time.sleep(5)
        else:
            raise ProxmoxOperationError(
                f"VM {vmid} did not reach 'running' state (current: {status})"
            )
        logger.info("  VM %d is running. Waiting %ds for Windows to start up ...", vmid, settle_seconds)
        time.sleep(settle_seconds)
        logger.info("  Ready to proceed.")

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
        logger.info("MIGRATION COMPLETE")
        logger.info("=" * 60)
        logger.info("")
        logger.info("The VM has been fully migrated to Proxmox.")
        logger.info("  - VirtIO drivers installed")
        logger.info("  - VMware Tools removed")
        logger.info("  - Network configuration restored")
        logger.info("  - All NICs enabled")
