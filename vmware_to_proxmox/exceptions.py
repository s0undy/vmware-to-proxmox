"""Custom exception hierarchy for the migration tool."""


class MigrationError(Exception):
    """Base exception for all migration errors."""


class VCenterConnectionError(MigrationError):
    """Cannot connect to vCenter."""


class VCenterOperationError(MigrationError):
    """A vCenter API operation failed."""


class GuestOperationError(MigrationError):
    """Running a script inside the guest VM failed."""


class GuestToolsNotRunning(GuestOperationError):
    """VMware Tools is not running in the guest."""


class ProxmoxConnectionError(MigrationError):
    """Cannot connect to Proxmox."""


class ProxmoxOperationError(MigrationError):
    """A Proxmox API operation failed."""


class ConfigurationError(MigrationError):
    """Invalid or missing configuration."""
