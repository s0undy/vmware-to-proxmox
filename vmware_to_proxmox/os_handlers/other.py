"""'Other' OS handler — skip all OS-specific steps (appliances, unknown OS)."""

import logging

from .base import OSHandler, StepContext

logger = logging.getLogger(__name__)


class OtherHandler(OSHandler):

    @property
    def os_label(self) -> str:
        return "Other/Appliance"

    def step_3_export_nic_config(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped — OS type is 'other'.")

    def step_4_enable_boot_driver(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped — OS type is 'other'.")

    def step_5_install_virtio_tools(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped — OS type is 'other'.")

    def step_12_install_virtio_drivers(self, ctx: StepContext):
        ctx.log.info("  Skipped — OS type is 'other'.")

    def step_13_purge_vmware_tools(self, ctx: StepContext):
        ctx.log.info("  Skipped — OS type is 'other'.")

    def step_14_restore_nic_config(self, ctx: StepContext):
        ctx.log.info("  Skipped — OS type is 'other'.")
