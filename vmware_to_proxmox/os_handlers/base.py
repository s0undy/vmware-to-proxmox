"""Abstract base class for OS-specific migration handlers."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class StepContext:
    """Bundles all arguments needed by OS handler steps 12-14."""
    vmid: int
    px: object  # ProxmoxClient (avoid circular import)
    config: object  # AppConfig
    dry_run: bool
    wait_for_vm_ready: Callable
    effective_wait: Callable
    sleep_fn: Callable
    log: logging.Logger | logging.LoggerAdapter = None

    def __post_init__(self):
        if self.log is None:
            self.log = logger


class OSHandler(ABC):
    """Strategy interface for OS-specific migration steps."""

    @property
    @abstractmethod
    def os_label(self) -> str:
        """Human-readable OS label for logging (e.g., 'Windows', 'Ubuntu')."""
        ...

    @abstractmethod
    def step_3_export_nic_config(self, vm, guest_ops, config, dry_run: bool) -> None:
        """Export NIC configuration from the guest before migration."""
        ...

    @abstractmethod
    def step_4_enable_boot_driver(self, vm, guest_ops, config, dry_run: bool) -> None:
        """Enable the VirtIO SCSI boot driver (or equivalent) in the guest."""
        ...

    @abstractmethod
    def step_5_install_virtio_tools(self, vm, guest_ops, config, dry_run: bool) -> None:
        """Install VirtIO guest tools/agents before shutdown."""
        ...

    @abstractmethod
    def step_12_install_virtio_drivers(self, ctx: "StepContext") -> None:
        """Install VirtIO drivers from ISO after VM is running on Proxmox."""
        ...

    @abstractmethod
    def step_13_purge_vmware_tools(self, ctx: "StepContext") -> None:
        """Remove VMware Tools from the guest."""
        ...

    @abstractmethod
    def step_14_restore_nic_config(self, ctx: "StepContext") -> None:
        """Restore NIC configuration in the guest."""
        ...

    # ------------------------------------------------------------------
    # Shared helpers for steps 12-14
    # ------------------------------------------------------------------

    def _wait_and_connect_agent(self, ctx: "StepContext") -> None:
        """Wait for the VM to be ready, then wait for the QEMU guest agent."""
        settle = 10 if ctx.config.migration.enable_nics_on_boot else 30
        ctx.wait_for_vm_ready(ctx.vmid, settle_seconds=settle)
        ctx.log.info("  Waiting for QEMU guest agent ...")
        ctx.px.wait_for_guest_agent(ctx.vmid)
        ctx.log.info("  Guest agent is responding.")

    def _reboot_and_wait(self, ctx: "StepContext", pre_seconds: int, post_seconds: int) -> None:
        """Reboot the VM with pre/post wait periods."""
        ctx.log.info("  Waiting %ds before reboot ...", ctx.effective_wait(pre_seconds))
        ctx.sleep_fn(pre_seconds)
        ctx.px.reboot_vm(ctx.vmid)
        ctx.log.info("  Waiting %ds for VM to boot ...", ctx.effective_wait(post_seconds))
        ctx.sleep_fn(post_seconds)
