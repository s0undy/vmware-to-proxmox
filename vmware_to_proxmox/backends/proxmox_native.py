"""Default disk-migration backend: current Proxmox API + SSH behavior."""

import time

from ..exceptions import MigrationError, ProxmoxOperationError
from .base import BackendContext, DiskMigrationBackend


class ProxmoxNativeBackend(DiskMigrationBackend):
    """Steps 6-10 implemented against the Proxmox API and SSH/SFTP."""

    name = "proxmox-native"

    def step_6_shutdown(self, ctx: BackendContext, vm) -> None:
        from ..migration import SHUTDOWN_SETTLE_SECONDS

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would shut down %s", vm.name)
            return

        ctx.log.info("  Sending shutdown signal ...")
        ctx.vc.shutdown_guest(vm)
        ctx.log.info("  VM is powered off.")
        ctx.log.info("  Waiting %ds for clean shutdown ...", SHUTDOWN_SETTLE_SECONDS)
        time.sleep(SHUTDOWN_SETTLE_SECONDS)

    def step_7_rewrite_vmdk_descriptors(self, ctx: BackendContext, vm) -> None:
        vm_config = ctx.resolve_vm_config(vm)
        vmid = ctx.resolve_vmid()

        if ctx.dry_run:
            for i, d in enumerate(vm_config["disks"]):
                ctx.log.info("  DRY RUN: would rewrite scsi%d: %s -> vm-%d-disk-%d.vmdk",
                             i, d["filename"], vmid, i)
            return

        ctx.px.rewrite_vmdk_descriptors(
            vmid=vmid,
            vm_config=vm_config,
            storage_name=ctx.config.migration.proxmox_storage,
        )
        ctx.log.info("  All VMDK descriptors rewritten.")

    def step_8_start_vm(self, ctx: BackendContext, vm) -> None:
        from ..migration import VM_START_SETTLE_SECONDS

        vmid = ctx.resolve_vmid()
        start_before = ctx.config.migration.start_vm_before_move

        if not start_before:
            ctx.log.info("  start_vm_before_move=false — VM will start after disks are moved.")
            return

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would start VMID %d", vmid)
            return

        ctx.px.start_vm(vmid)
        ctx.log.info("  VM %d start command sent. Waiting %ds for VM to boot ...",
                     vmid, ctx.effective_wait(VM_START_SETTLE_SECONDS))
        ctx.sleep_fn(VM_START_SETTLE_SECONDS)
        ctx.log.info("  Ready to proceed.")

    def step_9_move_disks(self, ctx: BackendContext, vm) -> None:
        from ..migration import VM_START_SETTLE_SECONDS

        vmid = ctx.resolve_vmid()
        vm_config = ctx.resolve_vm_config(vm)
        final_storage = ctx.config.migration.proxmox_final_storage
        start_before = ctx.config.migration.start_vm_before_move
        disk_move_timeout = ctx.config.migration.disk_move_timeout

        if not final_storage:
            raise MigrationError(
                "Cannot move disks: proxmox_final_storage is not set. "
                "Set --proxmox-final-storage or proxmox_final_storage in config."
            )

        disks_to_move = [f"scsi{i}" for i in range(len(vm_config["disks"]))]
        if vm_config["firmware"] == "efi":
            disks_to_move.append("efidisk0")

        total = len(disks_to_move)
        ctx.log.info("  %d disk(s) to move (timeout %ds per disk).", total, disk_move_timeout)

        if not ctx.dry_run:
            px_config = ctx.px.get_vm_config_proxmox(vmid)

        for idx, disk_name in enumerate(disks_to_move, start=1):
            pct = int(idx / total * 100)
            if ctx.dry_run:
                ctx.log.info("  DRY RUN: [%d/%d %3d%%] would move %s -> %s (qcow2)",
                             idx, total, pct, disk_name, final_storage)
                continue

            current_value = px_config.get(disk_name, "")
            if current_value.startswith(f"{final_storage}:"):
                ctx.log.info("  [%d/%d %3d%%] %s already on %s — skipping.",
                             idx, total, pct, disk_name, final_storage)
                continue

            ctx.log.info("  [%d/%d %3d%%] Moving %s ...", idx, total, pct, disk_name)
            ctx.px.move_disk(vmid, disk_name, final_storage, timeout=disk_move_timeout)

        ctx.log.info("  All disks moved to %s.", final_storage)

        if not start_before:
            if ctx.dry_run:
                ctx.log.info("  DRY RUN: would start VMID %d after move", vmid)
                return
            ctx.log.info("  Starting VM after disk move ...")
            ctx.px.start_vm(vmid)
            ctx.log.info("  VM %d start command sent. Waiting %ds for VM to boot ...",
                         vmid, ctx.effective_wait(VM_START_SETTLE_SECONDS))
            ctx.sleep_fn(VM_START_SETTLE_SECONDS)
            ctx.log.info("  Ready to proceed.")

    def step_11_verify(self, ctx: BackendContext, vm) -> None:
        from ..migration import VM_FULL_BOOT_SECONDS

        vmid = ctx.resolve_vmid()
        vm_config = ctx.resolve_vm_config(vm)
        final_storage = ctx.config.migration.proxmox_final_storage

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would verify VMID %d is running on %s", vmid, final_storage)
            return

        status = ctx.px.get_vm_status(vmid)
        ctx.log.info("  VM %d status: %s", vmid, status)
        if status != "running":
            raise ProxmoxOperationError(
                f"VM {vmid} is not running (status: {status})"
            )

        px_config = ctx.px.get_vm_config_proxmox(vmid)
        disk_keys = [f"scsi{i}" for i in range(len(vm_config["disks"]))]
        if vm_config["firmware"] == "efi":
            disk_keys.append("efidisk0")

        for key in disk_keys:
            value = px_config.get(key, "")
            if not value.startswith(f"{final_storage}:"):
                raise ProxmoxOperationError(
                    f"Disk {key} is not on final storage {final_storage}: {value}"
                )
            ctx.log.info("  %s: %s", key, value)

        ctx.log.info("  Verification passed — all disks on %s, VM running.", final_storage)

        ctx.log.info("  Waiting %ds for VM to fully boot ...", ctx.effective_wait(VM_FULL_BOOT_SECONDS))
        ctx.sleep_fn(VM_FULL_BOOT_SECONDS)
