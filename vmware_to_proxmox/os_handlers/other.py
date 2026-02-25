"""'Other' OS handler — skip all OS-specific steps (appliances, unknown OS)."""

import logging

from .base import OSHandler

logger = logging.getLogger(__name__)


class OtherHandler(OSHandler):

    @property
    def os_label(self) -> str:
        return "Other/Appliance"

    def step_3_export_nic_config(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped (OS type: other)")

    def step_4_enable_boot_driver(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped (OS type: other)")

    def step_5_install_virtio_tools(self, vm, guest_ops, config, dry_run):
        logger.info("  Skipped (OS type: other)")

    def step_11_install_virtio_drivers(self, vmid, px, config, dry_run,
                                       wait_for_vm_ready, effective_wait, sleep_fn):
        logger.info("  Skipped (OS type: other)")

    def step_12_purge_vmware_tools(self, vmid, px, config, dry_run,
                                   wait_for_vm_ready, effective_wait, sleep_fn):
        logger.info("  Skipped (OS type: other)")

    def step_13_restore_nic_config(self, vmid, px, config, dry_run,
                                   wait_for_vm_ready, effective_wait, sleep_fn):
        logger.info("  Skipped (OS type: other)")
