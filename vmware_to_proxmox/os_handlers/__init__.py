"""OS handler factory and auto-detection."""

from .base import OSHandler
from .other import OtherHandler
from .windows import WindowsHandler
from .ubuntu import UbuntuHandler

# Registry of known OS types -> handler classes.
# Add new OS types here.
_OS_HANDLERS = {
    "windows": WindowsHandler,
    "ubuntu": UbuntuHandler,
    "other": OtherHandler,
}


def get_os_handler(os_type: str) -> OSHandler:
    """Return an OS handler instance for the given os_type string."""
    handler_cls = _OS_HANDLERS.get(os_type.lower())
    if handler_cls is None:
        known = ", ".join(sorted(_OS_HANDLERS.keys()))
        raise ValueError(f"Unknown os_type '{os_type}'. Supported: {known}")
    return handler_cls()


# Pattern-based rules for auto-detecting OS type from vCenter guestId.
_GUEST_ID_PATTERNS = [
    ("windows", "windows"),
    ("ubuntu", "ubuntu"),
]


def detect_os_type(guest_id: str) -> str:
    """Detect the os_type from a vCenter guestId string.

    Returns 'windows', 'ubuntu', or 'other'.
    """
    guest_id_lower = guest_id.lower()
    for pattern, os_type in _GUEST_ID_PATTERNS:
        if pattern in guest_id_lower:
            return os_type
    return "other"
