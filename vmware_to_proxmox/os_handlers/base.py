"""Abstract base class for OS-specific migration handlers."""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


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
    def step_11_install_virtio_drivers(self, vmid, px, config, dry_run: bool,
                                       wait_for_vm_ready, effective_wait, sleep_fn) -> None:
        """Install VirtIO drivers from ISO after VM is running on Proxmox."""
        ...

    @abstractmethod
    def step_12_purge_vmware_tools(self, vmid, px, config, dry_run: bool,
                                   wait_for_vm_ready, effective_wait, sleep_fn) -> None:
        """Remove VMware Tools from the guest."""
        ...

    @abstractmethod
    def step_13_restore_nic_config(self, vmid, px, config, dry_run: bool,
                                   wait_for_vm_ready, effective_wait, sleep_fn) -> None:
        """Restore NIC configuration in the guest."""
        ...
