"""Abstract backend interface for the disk-migration phase (steps 6-10)."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..config import AppConfig
from ..proxmox import ProxmoxClient
from ..vcenter import VCenterClient


@dataclass
class BackendContext:
    """Handles the orchestrator passes to a backend for each step.

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
    """Owns steps 6-10: shutdown, descriptor rewrite, start, move, verify."""

    name: str = "base"

    def prepare(self, ctx: BackendContext) -> None:
        """Optional one-time setup after vCenter/Proxmox are connected."""

    @abstractmethod
    def step_6_shutdown(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_7_rewrite_vmdk_descriptors(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_8_start_vm(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_9_move_disks(self, ctx: BackendContext, vm) -> None: ...

    @abstractmethod
    def step_10_verify(self, ctx: BackendContext, vm) -> None: ...
