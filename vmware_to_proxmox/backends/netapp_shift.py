"""NetApp Shift backend — implements steps 6-11 via the NetApp Shift REST API.

Step 6 reuses the vCenter shutdown logic from ProxmoxNativeBackend. Steps 7-11
orchestrate the NetApp Shift lifecycle:

  7. Create the Resource Group (protectionGroup) for the VM.
  8. Create the Blueprint (drPlan) referencing that Resource Group.
  9. Trigger the disk conversion (bluePrint/{id}/convert/execution) and
     wait for it to reach a terminal state.
 10. Move the converted qcow2 files into the Proxmox images directory,
     attach them to the VM, allocate efidisk0 (if OVMF), and start it.
 11. Verify the VM is running with all disks on the final storage.

Splitting the conversion (step 9) and the import (step 10) lets the
operator rerun ``--skip-to 10`` after troubleshooting a failed
conversion — or after performing the conversion manually — without
re-triggering NetApp Shift.
"""

import time

from ..config import NetAppShiftConfig
from ..exceptions import MigrationError
from ..netapp_shift import NetAppShiftClient
from .base import BackendContext, DiskMigrationBackend

SOURCE_DISCOVERY_SETTLE_SECONDS = 30


class NetAppShiftBackend(DiskMigrationBackend):
    name = "netapp-shift"

    step_labels = {
        6: "Shut down VM",
        7: "Create NetApp Resource Group",
        8: "Create NetApp Blueprint",
        9: "Convert disks via NetApp Shift",
        10: "Import converted disks into Proxmox",
        11: "Verify VM on final storage",
    }

    def __init__(self, shift_config: NetAppShiftConfig):
        self.shift_config = shift_config
        self.client: NetAppShiftClient | None = None
        self._resource_group_id: str | None = None
        self._blueprint_id: str | None = None
        self._execution_id: str | None = None
        self._vm_info: dict | None = None
        self._source_site_id: str | None = None
        self._source_virt_env_id: str | None = None
        self._dest_site_id: str | None = None
        self._dest_virt_env_id: str | None = None

    def prepare(self, ctx: BackendContext) -> None:
        ctx.log.info("Initializing NetApp Shift backend ...")
        self.client = NetAppShiftClient(self.shift_config)
        self.client.connect()
        ctx.log.info("  NetApp Shift backend ready.")

    def finalize(self, ctx: BackendContext) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception as exc:
                ctx.log.warning("  NetApp Shift logout failed: %s", exc)
            self.client = None
        try:
            ctx.px.close_ssh()
        except Exception as exc:
            ctx.log.warning("  Proxmox SSH close failed: %s", exc)

    # ------------------------------------------------------------------
    # Step 6 — vCenter shutdown (mirrors ProxmoxNativeBackend)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Step 7 — create Resource Group
    # ------------------------------------------------------------------

    def step_7_rewrite_vmdk_descriptors(self, ctx: BackendContext, vm) -> None:
        """Step 7 (NetApp Shift): create the Resource Group.

        Pre-creates ``/mnt/pve/{proxmox_final_storage}/images/{vmid}`` on
        the Proxmox node so NetApp Shift (pointed at that path via the
        custom qtree's ``volumePath``) can write the converted qcow2 files
        directly into their final location — no post-conversion move.
        """
        cfg = ctx.config.migration
        vmid = ctx.resolve_vmid()
        rg_name = f"{vm.name}-rg"

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would ensure /mnt/pve/%s/images/%d exists and "
                "create resource group %s for VM %s (datastore=%s, volume=%s)",
                cfg.proxmox_final_storage, vmid, rg_name, vm.name,
                cfg.proxmox_final_storage, cfg.netapp_destination_volume,
            )
            return

        # Create the target directory before the resource group so the
        # custom-qtree volumePath references a real location. Idempotent,
        # safe to rerun after a partial step 7 failure.
        ctx.px.ensure_vm_image_dir(
            vmid=vmid, final_storage=cfg.proxmox_final_storage,
        )

        existing = self.client.get_resource_group_id_by_name(rg_name)
        if existing:
            ctx.log.info(
                "  Resource group %s already exists (id=%s) — reusing.",
                rg_name, existing,
            )
            self._resource_group_id = existing
            return

        self._source_site_id = self.client.get_site_id_by_name(cfg.netapp_source_site)
        self._source_virt_env_id = self.client.get_virt_env_id(self._source_site_id)
        self._dest_site_id = self.client.get_site_id_by_name(cfg.netapp_destination_site)
        self._dest_virt_env_id = self.client.get_virt_env_id(self._dest_site_id)

        # Refresh the source inventory so the unprotected-VM list reflects
        # the VM's current layout (disks, NICs, ...) before we snapshot it.
        ctx.log.info("  Triggering NetApp Shift source discovery ...")
        self.client.discover_source(self._source_site_id, self._source_virt_env_id)
        ctx.log.info(
            "  Waiting %ds for discovery to settle ...",
            ctx.effective_wait(SOURCE_DISCOVERY_SETTLE_SECONDS),
        )
        ctx.sleep_fn(SOURCE_DISCOVERY_SETTLE_SECONDS)

        self._vm_info = self.client.get_unprotected_vm_by_name(
            self._source_site_id, self._source_virt_env_id, vm.name,
        )

        ctx.log.info("  Creating resource group %s ...", rg_name)
        self._resource_group_id = self.client.create_resource_group(
            name=rg_name,
            source_site_id=self._source_site_id,
            source_virt_env_id=self._source_virt_env_id,
            dest_site_id=self._dest_site_id,
            dest_virt_env_id=self._dest_virt_env_id,
            vm_id=self._vm_info["_id"],
            vm_name=vm.name,
            vmid=vmid,
            datastore_name=cfg.proxmox_final_storage,
            volume_name=cfg.netapp_destination_volume,
        )
        ctx.log.info("  Resource group created (id=%s).", self._resource_group_id)

    # ------------------------------------------------------------------
    # Step 8 — create Blueprint
    # ------------------------------------------------------------------

    def step_8_start_vm(self, ctx: BackendContext, vm) -> None:
        """Step 8 (NetApp Shift): create the Blueprint."""
        cfg = ctx.config.migration
        bp_name = f"{vm.name}-bp"
        rg_name = f"{vm.name}-rg"

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would create blueprint %s referencing resource group %s",
                bp_name, rg_name,
            )
            return

        existing_bp = self.client.get_blueprint_id_by_name(bp_name)
        if existing_bp:
            ctx.log.info(
                "  Blueprint %s already exists (id=%s) — reusing.",
                bp_name, existing_bp,
            )
            self._blueprint_id = existing_bp
            return

        if self._resource_group_id is None:
            self._resource_group_id = self.client.get_resource_group_id_by_name(rg_name)
            if self._resource_group_id is None:
                raise MigrationError(
                    f"Cannot create blueprint: resource group {rg_name} not found "
                    "(re-run from step 7)."
                )

        if self._source_site_id is None:
            self._source_site_id = self.client.get_site_id_by_name(cfg.netapp_source_site)
        if self._source_virt_env_id is None:
            self._source_virt_env_id = self.client.get_virt_env_id(self._source_site_id)
        if self._dest_site_id is None:
            self._dest_site_id = self.client.get_site_id_by_name(cfg.netapp_destination_site)
        if self._dest_virt_env_id is None:
            self._dest_virt_env_id = self.client.get_virt_env_id(self._dest_site_id)

        # The VM is no longer in the unprotected list once step 7 has placed
        # it in a resource group, so on resume we read the VM info back from
        # the existing resource group instead of /api/setup/vm/unprotected.
        if self._vm_info is None:
            self._vm_info = self.client.get_resource_group_vm_info(rg_name)
            if self._vm_info is None:
                raise MigrationError(
                    f"Cannot create blueprint: VM {vm.name} not found in "
                    f"resource group {rg_name}."
                )

        ctx.log.info("  Creating blueprint %s ...", bp_name)
        self._blueprint_id = self.client.create_blueprint(
            name=bp_name,
            source_site_id=self._source_site_id,
            source_virt_env_id=self._source_virt_env_id,
            dest_site_id=self._dest_site_id,
            dest_virt_env_id=self._dest_virt_env_id,
            resource_group_id=self._resource_group_id,
            vm_info=self._vm_info,
        )
        ctx.log.info("  Blueprint created (id=%s).", self._blueprint_id)

    # ------------------------------------------------------------------
    # Step 9 — trigger conversion and wait for it
    # ------------------------------------------------------------------

    def step_9_move_disks(self, ctx: BackendContext, vm) -> None:
        """Step 9 (NetApp Shift): trigger conversion and wait for completion."""
        cfg = ctx.config.migration
        bp_name = f"{vm.name}-bp"

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would trigger conversion for blueprint %s and wait "
                "for completion.",
                bp_name,
            )
            return

        if self._blueprint_id is None:
            self._blueprint_id = self.client.get_blueprint_id_by_name(bp_name)
            if self._blueprint_id is None:
                raise MigrationError(
                    f"Cannot trigger conversion: blueprint {bp_name} not found "
                    "(re-run from step 8)."
                )

        if self._execution_id is None:
            ctx.log.info(
                "  Triggering NetApp Shift conversion for blueprint %s ...",
                self._blueprint_id,
            )
            self._execution_id = self.client.trigger_conversion(self._blueprint_id)
            ctx.log.info(
                "  Conversion triggered (execution id: %s).", self._execution_id,
            )
        else:
            ctx.log.info(
                "  Reusing existing execution id %s (skipping trigger).",
                self._execution_id,
            )

        timeout = cfg.disk_move_timeout
        ctx.log.info(
            "  Polling NetApp Shift conversion status (timeout %ds) ...", timeout,
        )
        self.client.wait_for_execution(self._execution_id, timeout=timeout)
        ctx.log.info("  Conversion finished successfully.")

    # ------------------------------------------------------------------
    # Step 10 — import converted qcow2 files into Proxmox and start VM
    # ------------------------------------------------------------------

    def step_10_import_disks(self, ctx: BackendContext, vm) -> None:
        """Step 10 (NetApp Shift): move converted qcow2 files into Proxmox.

        This step is independent of step 9 so the operator can rerun it
        with ``--skip-to 10`` after either troubleshooting a failed
        conversion or performing the conversion manually.
        """
        from ..migration import VM_START_SETTLE_SECONDS

        cfg = ctx.config.migration
        vmid = ctx.resolve_vmid()
        vm_config = ctx.resolve_vm_config(vm)
        num_disks = len(vm_config["disks"])

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would import %d converted disk(s) for %s into VMID %d "
                "and start the VM.",
                num_disks, vm.name, vmid,
            )
            return

        ctx.log.info(
            "  Importing converted disks for %s (VMID %d) ...", vm.name, vmid,
        )
        ctx.px.import_disks_from_netapp_shift(
            vmid=vmid,
            vm_name=vm.name,
            num_disks=num_disks,
            firmware=vm_config["firmware"],
            final_storage=cfg.proxmox_final_storage,
        )

        ctx.log.info("  Starting VMID %d ...", vmid)
        ctx.px.start_vm(vmid)
        ctx.log.info(
            "  Start command sent. Waiting %ds for VM to boot ...",
            ctx.effective_wait(VM_START_SETTLE_SECONDS),
        )
        ctx.sleep_fn(VM_START_SETTLE_SECONDS)
        ctx.log.info("  Ready to proceed.")

    # ------------------------------------------------------------------
    # Step 11 — verify VM on final storage
    # ------------------------------------------------------------------

    def step_11_verify(self, ctx: BackendContext, vm) -> None:
        """Step 11 (NetApp Shift): verify VM is running with expected disks."""
        from ..exceptions import ProxmoxOperationError
        from ..migration import VM_FULL_BOOT_SECONDS

        vmid = ctx.resolve_vmid()
        vm_config = ctx.resolve_vm_config(vm)
        final_storage = ctx.config.migration.proxmox_final_storage

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would verify VMID %d is running on %s",
                vmid, final_storage,
            )
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

        ctx.log.info(
            "  Verification passed — all disks on %s, VM running.", final_storage,
        )
        ctx.log.info(
            "  Waiting %ds for VM to fully boot ...",
            ctx.effective_wait(VM_FULL_BOOT_SECONDS),
        )
        ctx.sleep_fn(VM_FULL_BOOT_SECONDS)
