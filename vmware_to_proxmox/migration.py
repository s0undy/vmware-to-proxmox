"""Migration orchestrator — ties all fifteen steps together."""

import logging
import time

from .backends import BackendContext, get_backend
from .config import AppConfig
from .exceptions import MigrationError, ProxmoxOperationError, ProxmoxConnectionError
from .guest_ops import GuestOperations
from .os_handlers import detect_os_type, get_os_handler
from .os_handlers.base import OSHandler, StepContext
from .proxmox import ProxmoxClient
from .vcenter import VCenterClient

logger = logging.getLogger(__name__)


class _VMLoggerAdapter(logging.LoggerAdapter):
    """Prefixes all log messages with [vm_name] for multi-VM clarity."""

    def process(self, msg, kwargs):
        return f"[{self.extra['vm']}] {msg}", kwargs


TOTAL_STEPS = 15

# -- Timing constants (seconds) ------------------------------------------------
SHUTDOWN_SETTLE_SECONDS = 15       # Post-shutdown grace before modifying Proxmox
VM_START_SETTLE_SECONDS = 30       # Wait after VM power-on command
VM_FULL_BOOT_SECONDS = 40         # Full boot after verification
VIRTIO_INSTALL_SETTLE_SECONDS = 20   # VM settle after VirtIO driver install
ISO_MOUNT_WAIT_SECONDS = 5         # ISO availability after mount
PRE_REBOOT_PAUSE_SECONDS = 10      # Grace period before reboot (step 13)
POST_REBOOT_BOOT_SECONDS = 40      # Boot after reboot (steps 13, 14)
NIC_RESTORE_PRE_REBOOT_SECONDS = 15  # Grace period before reboot (step 14)
REBOOT_INITIATION_SECONDS = 15     # Delay before polling after reboot command
PRE_FINALIZE_SETTLE_SECONDS = 15   # Extra settle time before step 15
VM_READY_TIMEOUT_SECONDS = 300     # Max wait for VM to reach 'running'
VM_READY_POLL_SECONDS = 5          # Poll interval for VM ready check


class MigrationOrchestrator:
    def __init__(self, config: AppConfig, skip_to: int = 1, dry_run: bool = False,
                 os_handler: OSHandler | None = None):
        self.config = config
        self.skip_to = skip_to
        self.dry_run = dry_run
        self.os_handler = os_handler
        self.vc = VCenterClient(config.vcenter)
        self.px = ProxmoxClient(config.proxmox)
        self.guest_ops = None
        self.log = _VMLoggerAdapter(logger, {"vm": config.migration.vm_name})
        self.backend = get_backend(config)

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

        self.log.info("=" * 60)
        self.log.info("VMware-to-Proxmox Migration")
        self.log.info("  VM:                  %s", vm_name)
        self.log.info("  Migration datastore: %s", self.config.migration.migration_datastore)
        self.log.info("  Proxmox storage:     %s", self.config.migration.proxmox_storage)
        if self.config.migration.proxmox_final_storage:
            self.log.info("  Proxmox final store: %s", self.config.migration.proxmox_final_storage)
        if self.dry_run:
            self.log.info("  *** DRY RUN — no changes will be made ***")
        if self.config.migration.enable_nics_on_boot:
            self.log.info("  NICs on boot:        enabled (reduced wait timers)")
        if self.config.migration.enable_ha:
            self.log.info("  HA:                  enabled")
        if self.skip_to > 1:
            self.log.info("  Resuming from step %d", self.skip_to)
        self.log.info("=" * 60)

        # Validate skip-to prerequisites early
        if self.skip_to > 2 and not self.config.migration.proxmox_vmid:
            raise MigrationError(
                f"--skip-to {self.skip_to} requires --proxmox-vmid (or proxmox_vmid in config) "
                "because step 2 (Create Proxmox VM) is skipped."
            )

        self._connect()

        vm = self.vc.get_vm_by_name(vm_name)
        self.log.info("Found VM: %s  (power: %s)", vm.name, vm.runtime.powerState)

        # Auto-detect OS type from vCenter guestId if not set explicitly
        if self.os_handler is None:
            guest_id = vm.config.guestId
            detected = detect_os_type(guest_id)
            self.log.info("  Auto-detected OS type: %s  (guestId: %s)", detected, guest_id)
            self.os_handler = get_os_handler(detected)
        self.log.info("  OS handler:          %s", self.os_handler.os_label)
        self.log.info("  Disk backend:        %s", self.backend.name)

        self.backend.prepare(self._build_backend_context())

        try:
            return self._run_steps(vm, vm_name, start_time)
        finally:
            self.backend.finalize(self._build_backend_context())

    def _run_steps(self, vm, vm_name: str, start_time: float) -> dict:
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
            (10, "Import converted disks", self._step_10_import_disks),
            (11, "Verify VM on final storage", self._step_11_verify),
            (12, "Install VirtIO drivers from ISO", self._step_12_install_virtio_drivers),
            (13, "Purge VMware Tools", self._step_13_purge_vmware_tools),
            (14, "Restore NIC configuration", self._step_14_import_nic_config),
            (15, "Finalize VM", self._step_15_finalize),
        ]

        for num, label, fn in steps:
            if self.skip_to > num:
                self.log.info("")
                self.log.info("SKIP step %d/%d: %s", num, TOTAL_STEPS, label)
                continue
            self.log.info("")
            self.log.info("STEP %d/%d: %s", num, TOTAL_STEPS, label)
            self.log.info("-" * 40)
            step_start = time.monotonic()
            fn(vm)
            step_elapsed = time.monotonic() - step_start
            step_min, step_sec = divmod(int(step_elapsed), 60)
            self.log.info("  Step %d completed in %dm %ds", num, step_min, step_sec)

        # Enroll VM in Proxmox HA if configured (must be last, after all steps)
        if self.config.migration.enable_ha:
            vmid = self._resolve_vmid()
            self.log.info("")
            self.log.info("POST-MIGRATION: Adding VM to Proxmox HA")
            self.log.info("-" * 40)
            if self.dry_run:
                self.log.info("  DRY RUN: would add VMID %d to HA", vmid)
            else:
                try:
                    self.px.add_to_ha(vmid)
                    self.log.info("  VM %d enrolled in HA.", vmid)
                except ProxmoxOperationError as exc:
                    self.log.warning("  Failed to add VM to HA: %s", exc)
                    self.log.warning("  You can add it manually: ha-manager add vm:%d", vmid)

        # Query guest agent for primary IP address (skip when HA is enabled
        # because Proxmox may migrate the VM to another node after enrollment)
        ip_address = None
        if not self.dry_run and not self.config.migration.enable_ha:
            vmid = self._resolve_vmid()
            self._wait_for_vm_ready(vmid)
            self.log.info("  Waiting for QEMU guest agent ...")
            try:
                self.px.wait_for_guest_agent(vmid)
                ip_address = self.px.get_guest_ip(vmid)
            except (ProxmoxOperationError, ProxmoxConnectionError, OSError):
                self.log.warning("  Could not retrieve IP from guest agent.")

        elapsed = time.monotonic() - start_time
        minutes, seconds = divmod(int(elapsed), 60)

        self._print_next_steps()
        self.log.info("Migration of %s completed in %dm %ds", vm_name, minutes, seconds)

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
        self.log.info("Connecting to vCenter %s ...", self.config.vcenter.host)
        self.vc.connect()
        self.log.info("  Connected.")

        self.log.info("Connecting to Proxmox %s ...", self.config.proxmox.host)
        self.px.connect()
        self.log.info("  Connected.")

        # Guest operations require credentials — skip when none provided (e.g. os_type=other)
        if self.config.guest.user and self.config.guest.password:
            self.guest_ops = GuestOperations(self.vc, self.config.guest)

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    def _step_1_storage_vmotion(self, vm):
        ds = self.vc.get_datastore_by_name(self.config.migration.migration_datastore)
        self.log.info("  Target datastore: %s", ds.name)
        self.log.info("  Free space:       %.1f GB", ds.summary.freeSpace / (1024 ** 3))

        if self.vc.vm_is_on_datastore(vm, ds):
            self.log.info("  VM is already on datastore '%s' — skipping vMotion.", ds.name)
            return

        if self.dry_run:
            self.log.info("  DRY RUN: would relocate VM to %s", ds.name)
            return

        self.log.info("  Relocating VM (this may take a while) ...")
        self.vc.storage_vmotion(vm, ds)
        self.log.info("  Storage vMotion complete.")

    def _step_2_create_proxmox_vm(self, vm):
        vm_config = self.vc.get_vm_config(vm)
        self._vm_config = vm_config

        cpu_display = self.config.migration.cpu_type
        if self.config.migration.cpu_flags:
            cpu_display = f"{cpu_display},{self.config.migration.cpu_flags}"
        self.log.info("  CPUs:     %d  (%d sockets x %d cores)",
                      vm_config["num_cpus"],
                      max(1, vm_config["num_cpus"] // vm_config["num_cores_per_socket"]),
                      vm_config["num_cores_per_socket"])
        self.log.info("  CPU type: %s", cpu_display)
        self.log.info("  Memory:   %d MB", vm_config["memory_mb"])
        self.log.info("  Firmware: %s", vm_config["firmware"])
        for i, d in enumerate(vm_config["disks"]):
            self.log.info("  Disk scsi%d: %.1f GB  (%s)", i, d["size_gb"], d["label"])
        for i, n in enumerate(vm_config["nics"]):
            self.log.info("  NIC  net%d:  %s", i, n["label"])

        if self.dry_run:
            self.log.info("  DRY RUN: would create Proxmox VM")
            return

        vmid = self.px.create_vm(vm_config, self.config.migration)
        self._vmid = vmid
        self.log.info("  Proxmox VM created — VMID %d", vmid)

    def _step_3_export_nic_config(self, vm):
        self.os_handler.step_3_export_nic_config(
            vm, self.guest_ops, self.config, self.dry_run)

    def _step_4_enable_vioscsi(self, vm):
        self.os_handler.step_4_enable_boot_driver(
            vm, self.guest_ops, self.config, self.dry_run)

    def _step_5_install_virtio_tools(self, vm):
        self.os_handler.step_5_install_virtio_tools(
            vm, self.guest_ops, self.config, self.dry_run)

    def _step_6_shutdown(self, vm):
        self.backend.step_6_shutdown(self._build_backend_context(), vm)

    def _step_7_rewrite_vmdk_descriptors(self, vm):
        self.backend.step_7_rewrite_vmdk_descriptors(self._build_backend_context(), vm)

    def _step_8_start_vm(self, vm):
        self.backend.step_8_start_vm(self._build_backend_context(), vm)

    def _step_9_move_disks(self, vm):
        self.backend.step_9_move_disks(self._build_backend_context(), vm)

    def _step_10_import_disks(self, vm):
        self.backend.step_10_import_disks(self._build_backend_context(), vm)

    def _step_11_verify(self, vm):
        self.backend.step_11_verify(self._build_backend_context(), vm)

    def _build_backend_context(self) -> BackendContext:
        """Build a BackendContext for the disk-migration backend (steps 6-11)."""
        return BackendContext(
            vc=self.vc,
            px=self.px,
            config=self.config,
            dry_run=self.dry_run,
            log=self.log,
            resolve_vmid=self._resolve_vmid,
            resolve_vm_config=self._resolve_vm_config,
            effective_wait=self._effective_wait,
            sleep_fn=self._sleep,
        )

    def _build_step_context(self) -> StepContext:
        """Build a StepContext for OS handler steps 12-14."""
        return StepContext(
            vmid=self._resolve_vmid(),
            px=self.px,
            config=self.config,
            dry_run=self.dry_run,
            wait_for_vm_ready=self._wait_for_vm_ready,
            effective_wait=self._effective_wait,
            sleep_fn=self._sleep,
            log=self.log,
        )

    def _step_12_install_virtio_drivers(self, vm):
        self.os_handler.step_12_install_virtio_drivers(self._build_step_context())

    def _step_13_purge_vmware_tools(self, vm):
        self.os_handler.step_13_purge_vmware_tools(self._build_step_context())

    def _step_14_import_nic_config(self, vm):
        self.os_handler.step_14_restore_nic_config(self._build_step_context())

    def _step_15_finalize(self, vm):
        vmid = self._resolve_vmid()

        if self.dry_run:
            self.log.info("  DRY RUN: would unmount ISO, clean up unused disks, enable NICs, and reboot VMID %d", vmid)
            return

        # Extra settle time to ensure the VM config lock is released after
        # the reboot issued in step 14.
        self.log.info("  Waiting %ds for VM to fully settle ...",
                      self._effective_wait(PRE_FINALIZE_SETTLE_SECONDS))
        self._sleep(PRE_FINALIZE_SETTLE_SECONDS)

        # Unmount the VirtIO ISO
        self.px.unmount_iso(vmid)

        # Delete any unused (detached) disks left over from the migration
        self.px.delete_unused_disks(vmid)

        # Enable all NICs (only needed if they were created with link_down=1)
        if not self.config.migration.enable_nics_on_boot:
            px_config = self.px.get_vm_config_proxmox(vmid)
            nic_keys = sorted(k for k in px_config if k.startswith("net") and k[3:].isdigit())
            for nic_key in nic_keys:
                self.log.info("  Enabling %s ...", nic_key)
                self.px.set_nic_link_state(vmid, nic_key, link_down=False)
            self.log.info("  All NICs enabled.")
        else:
            self.log.info("  NICs already enabled (enable_nics_on_boot=true), skipping.")

        # Final reboot — skip if NICs were already enabled on boot,
        # because step 14 (NIC restore) already performed a reboot.
        if self.config.migration.enable_nics_on_boot:
            self.log.info("  Skipping final reboot (already rebooted in step 14).")
        else:
            self.px.reboot_vm(vmid)
            self.log.info("  Final reboot initiated. Waiting %ds for VM to boot ...",
                          self._effective_wait(20))
            self._sleep(20)
            os_label = self.os_handler.os_label if self.os_handler else "OS"
            self.log.info("  Waiting %ds for %s to start up ...",
                          self._effective_wait(10), os_label)
            self._sleep(10)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _effective_wait(self, base_seconds: float) -> int:
        """Return the effective wait time — halved when NICs are enabled on boot."""
        if self.config.migration.enable_nics_on_boot:
            return int(base_seconds / 2)
        return int(base_seconds)

    def _sleep(self, base_seconds: float) -> None:
        """Sleep for the given duration, halved if enable_nics_on_boot is set."""
        time.sleep(self._effective_wait(base_seconds))

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
        """Wait until Proxmox reports the VM as running, then wait extra time for the OS to boot."""
        self.log.info("  Waiting for VM %d to be running ...", vmid)
        start = time.monotonic()
        status = "unknown"
        while time.monotonic() - start < VM_READY_TIMEOUT_SECONDS:
            status = self.px.get_vm_status(vmid)
            if status == "running":
                break
            time.sleep(VM_READY_POLL_SECONDS)
        else:
            raise ProxmoxOperationError(
                f"VM {vmid} did not reach 'running' state within "
                f"{VM_READY_TIMEOUT_SECONDS}s (current: {status})"
            )
        os_label = self.os_handler.os_label if self.os_handler else "OS"
        self.log.info("  VM %d is running. Waiting %ds for %s to start up ...",
                      vmid, settle_seconds, os_label)
        time.sleep(settle_seconds)
        self.log.info("  Ready to proceed.")

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
        from .os_handlers.other import OtherHandler
        self.log.info("")
        self.log.info("=" * 60)
        self.log.info("MIGRATION COMPLETE")
        self.log.info("=" * 60)
        self.log.info("")
        self.log.info("The VM has been fully migrated to Proxmox.")
        if not isinstance(self.os_handler, OtherHandler):
            self.log.info("  - VirtIO drivers installed")
            self.log.info("  - VMware Tools removed")
            self.log.info("  - Network configuration restored")
        self.log.info("  - All NICs enabled")
        if self.config.migration.enable_ha:
            self.log.info("  - High Availability enabled")
