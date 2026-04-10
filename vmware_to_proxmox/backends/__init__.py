"""Pluggable disk-migration backends for steps 6-10."""

from ..config import AppConfig
from ..exceptions import ConfigurationError
from .base import BackendContext, DiskMigrationBackend
from .netapp_shift import NetAppShiftBackend
from .proxmox_native import ProxmoxNativeBackend


def get_backend(config: AppConfig) -> DiskMigrationBackend:
    """Construct the disk-migration backend selected in config."""
    name = config.migration.disk_conversion_backend
    if name == "proxmox-native":
        return ProxmoxNativeBackend()
    if name == "netapp-shift":
        if config.netapp_shift is None:
            raise ConfigurationError(
                "disk_conversion_backend='netapp-shift' requires a netapp_shift "
                "config section or --netapp-shift-* CLI flags"
            )
        return NetAppShiftBackend(config.netapp_shift)
    raise ConfigurationError(f"Unknown disk_conversion_backend: {name!r}")


__all__ = [
    "BackendContext",
    "DiskMigrationBackend",
    "NetAppShiftBackend",
    "ProxmoxNativeBackend",
    "get_backend",
]
