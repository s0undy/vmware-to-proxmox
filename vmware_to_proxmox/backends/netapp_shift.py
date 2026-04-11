"""NetApp Shift backend — implements steps 6-10 via the NetApp Shift REST API.

Step 6 reuses the vCenter shutdown logic from ProxmoxNativeBackend. Steps 7-10
orchestrate the NetApp Shift lifecycle:

  7. Create the Resource Group (protectionGroup) for the VM.
  8. Create the Blueprint (drPlan) referencing that Resource Group.
  9. Trigger the migration (migrate/execution).
 10. Poll until the migration reaches a terminal status.

Moving the converted qcow2 into Proxmox is intentionally out of scope here —
it will be wired up in a follow-up change.
"""

import time

from ..config import NetAppShiftConfig
from ..exceptions import MigrationError
from ..netapp_shift import NetAppShiftClient
from .base import BackendContext, DiskMigrationBackend


class NetAppShiftBackend(DiskMigrationBackend):
    name = "netapp-shift"

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
        """Step 7 (NetApp Shift): create the Resource Group."""
        cfg = ctx.config.migration
        rg_name = f"{vm.name}-rg"

        if ctx.dry_run:
            ctx.log.info(
                "  DRY RUN: would create resource group %s for VM %s "
                "(datastore=%s, volume=%s, qtree=%s)",
                rg_name, vm.name, cfg.proxmox_final_storage,
                cfg.netapp_destination_volume, cfg.netapp_destination_qtree,
            )
            return

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
            datastore_name=cfg.proxmox_final_storage,
            volume_name=cfg.netapp_destination_volume,
            qtree_name=cfg.netapp_destination_qtree,
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
    # Step 9 — trigger migration
    # ------------------------------------------------------------------

    def step_9_move_disks(self, ctx: BackendContext, vm) -> None:
        """Step 9 (NetApp Shift): trigger the migration execution."""
        bp_name = f"{vm.name}-bp"

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would trigger migration for blueprint %s", bp_name)
            return

        if self._blueprint_id is None:
            self._blueprint_id = self.client.get_blueprint_id_by_name(bp_name)
            if self._blueprint_id is None:
                raise MigrationError(
                    f"Cannot trigger migration: blueprint {bp_name} not found "
                    "(re-run from step 8)."
                )

        ctx.log.info(
            "  Triggering NetApp Shift migration for blueprint %s ...",
            self._blueprint_id,
        )
        self._execution_id = self.client.trigger_migration(self._blueprint_id)
        ctx.log.info("  Migration triggered (execution id: %s).", self._execution_id)

    # ------------------------------------------------------------------
    # Step 10 — poll until terminal status
    # ------------------------------------------------------------------

    def step_10_verify(self, ctx: BackendContext, vm) -> None:
        """Step 10 (NetApp Shift): poll until the migration finishes."""
        bp_name = f"{vm.name}-bp"
        timeout = ctx.config.migration.disk_move_timeout

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would poll blueprint %s until completion", bp_name)
            return

        if self._blueprint_id is None:
            self._blueprint_id = self.client.get_blueprint_id_by_name(bp_name)
            if self._blueprint_id is None:
                raise MigrationError(
                    f"Cannot poll migration: blueprint {bp_name} not found."
                )

        ctx.log.info(
            "  Polling NetApp Shift migration status (timeout %ds) ...", timeout,
        )
        status = self.client.wait_for_migration(self._blueprint_id, timeout=timeout)
        if "complete" not in status:
            raise MigrationError(
                f"NetApp Shift migration ended with status: {status}"
            )
        ctx.log.info("  Migration finished — status: %s", status)
        ctx.log.info(
            "  Note: moving the converted qcow2 into Proxmox is a separate "
            "follow-up step (not yet implemented)."
        )
