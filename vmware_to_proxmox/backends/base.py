"""Abstract backend interface for the disk-migration phase (steps 6-11)."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..config import AppConfig
from ..proxmox import ProxmoxClient
from ..vcenter import VCenterClient


@dataclass
class BackendContext:
    """Bundles the arguments the orchestrator passes to a backend for each step.

    Mirrors the StepContext used by OS handlers so the two strategy seams
    look consistent.
    """
    vc: VCenterClient
    px: ProxmoxClient
    config: AppConfig
    dry_run: bool
    log: logging.LoggerAdapter
    resolve_vmid: Callable[[], int]
    resolve_vm_config: Callable[[object], dict]
    effective_wait: Callable[[int], int]
    sleep_fn: Callable[[int], None]


class DiskMigrationBackend(ABC):
    """Owns steps 6-11: shutdown, descriptor rewrite, start, move,
    import converted disks, verify.
    """

    name: str = "base"

    # Console labels for the backend-owned steps (6-11). Subclasses override
    # so the STEP N/15 banner matches what the backend actually does.
    step_labels: dict[int, str] = {}

    def prepare(self, ctx: BackendContext) -> None:
        """Optional one-time setup after vCenter/Proxmox are connected."""

    def finalize(self, ctx: BackendContext) -> None:
        """Optional teardown — release any backend-owned resources.

        Called from a ``finally`` block, so it runs on both successful
        and failed migrations. Implementations must be safe to call when
        ``prepare`` only partially succeeded.
        """

    @abstractmethod
    def step_6_shutdown(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_7_rewrite_vmdk_descriptors(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_8_start_vm(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_9_move_disks(self, ctx: BackendContext, vm) -> None: ...

    def step_10_import_disks(self, ctx: BackendContext, vm) -> None:
        """Import externally converted disks into the Proxmox VM.

        Default: no-op. Backends that perform disk conversion outside
        Proxmox (e.g. NetApp Shift) override this to move the resulting
        files into the Proxmox images directory and attach them.
        """

    @abstractmethod
    def step_11_verify(self, ctx: BackendContext, vm) -> None: ...
