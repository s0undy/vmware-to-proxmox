"""vCenter / pyvmomi operations."""

import atexit
import logging
import ssl
import time

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

from .config import VCenterConfig
from .exceptions import VCenterConnectionError, VCenterOperationError

logger = logging.getLogger(__name__)


class VCenterClient:
    def __init__(self, config: VCenterConfig):
        self.config = config
        self.si = None
        self.content = None

    def connect(self):
        """Establish a connection to vCenter."""
        ctx = None
        if self.config.insecure:
            ctx = ssl._create_unverified_context()
        try:
            self.si = SmartConnect(
                host=self.config.host,
                user=self.config.user,
                pwd=self.config.password,
                port=self.config.port,
                sslContext=ctx,
            )
        except Exception as exc:
            raise VCenterConnectionError(
                f"Failed to connect to vCenter {self.config.host}: {exc}"
            ) from exc
        atexit.register(Disconnect, self.si)
        self.content = self.si.RetrieveContent()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_vm_by_name(self, name: str) -> vim.VirtualMachine:
        """Find a VM by name using a ContainerView."""
        container = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], True,
        )
        try:
            for obj in container.view:
                if obj.name == name:
                    return obj
        finally:
            container.Destroy()
        raise VCenterOperationError(f"VM '{name}' not found in vCenter")

    def get_datastore_by_name(self, name: str) -> vim.Datastore:
        """Find a datastore by name using a ContainerView."""
        container = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.Datastore], True,
        )
        try:
            for obj in container.view:
                if obj.name == name:
                    return obj
        finally:
            container.Destroy()
        raise VCenterOperationError(f"Datastore '{name}' not found in vCenter")

    # ------------------------------------------------------------------
    # VM inspection
    # ------------------------------------------------------------------

    def get_vm_config(self, vm: vim.VirtualMachine) -> dict:
        """Extract VM hardware configuration into a plain dict.

        Returns a dict with keys: name, num_cpus, num_cores_per_socket,
        memory_mb, firmware, guest_id, disks (list), nics (list).
        Disks are sorted by (controller_key, unit_number).
        NICs are sorted by unit_number.
        """
        cfg = vm.config
        hw = cfg.hardware

        result = {
            "name": vm.name,
            "num_cpus": hw.numCPU,
            "num_cores_per_socket": hw.numCoresPerSocket,
            "memory_mb": hw.memoryMB,
            "firmware": cfg.firmware,  # "bios" or "efi"
            "guest_id": cfg.guestId,
            "disks": [],
            "nics": [],
        }

        for device in hw.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                result["disks"].append({
                    "label": device.deviceInfo.label,
                    "size_bytes": device.capacityInBytes,
                    "size_gb": device.capacityInBytes / (1024 ** 3),
                    "unit_number": device.unitNumber,
                    "controller_key": device.controllerKey,
                    "thin": getattr(backing, "thinProvisioned", False),
                    "filename": getattr(backing, "fileName", ""),
                })
            elif isinstance(device, vim.vm.device.VirtualEthernetCard):
                network_name = ""
                if hasattr(device.backing, "network") and device.backing.network:
                    network_name = device.backing.network.name
                elif hasattr(device.backing, "port"):
                    network_name = device.backing.port.portgroupKey
                result["nics"].append({
                    "label": device.deviceInfo.label,
                    "mac": device.macAddress,
                    "network": network_name,
                    "unit_number": device.unitNumber,
                })

        result["disks"].sort(key=lambda d: (d["controller_key"], d["unit_number"]))
        result["nics"].sort(key=lambda n: n["unit_number"])
        return result

    # ------------------------------------------------------------------
    # Datastore checks
    # ------------------------------------------------------------------

    def vm_is_on_datastore(
        self, vm: vim.VirtualMachine, datastore: vim.Datastore,
    ) -> bool:
        """Return True if ALL VM disks are already on the given datastore."""
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                if hasattr(backing, "datastore") and backing.datastore:
                    if backing.datastore.name != datastore.name:
                        return False
                else:
                    return False
        return True

    # ------------------------------------------------------------------
    # Storage vMotion
    # ------------------------------------------------------------------

    def storage_vmotion(
        self,
        vm: vim.VirtualMachine,
        datastore: vim.Datastore,
        timeout_seconds: int = 7200,
    ) -> None:
        """Relocate all VM disks and config to the target datastore."""
        # Check free space
        total_disk = sum(
            d.capacityInBytes
            for d in vm.config.hardware.device
            if isinstance(d, vim.vm.device.VirtualDisk)
        )
        free = datastore.summary.freeSpace
        if free < total_disk:
            raise VCenterOperationError(
                f"Insufficient space on '{datastore.name}': "
                f"need {total_disk / (1024**3):.1f} GB, "
                f"have {free / (1024**3):.1f} GB free"
            )

        spec = vim.vm.RelocateSpec()
        spec.datastore = datastore

        # Explicitly relocate every disk
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                locator = vim.vm.RelocateSpec.DiskLocator()
                locator.datastore = datastore
                locator.diskId = device.key
                spec.disk.append(locator)

        task = vm.RelocateVM_Task(spec=spec)
        self._wait_for_task(task, "Storage vMotion", timeout_seconds)

    # ------------------------------------------------------------------
    # Power operations
    # ------------------------------------------------------------------

    def shutdown_guest(
        self, vm: vim.VirtualMachine, timeout_seconds: int = 300,
    ) -> None:
        """Graceful guest OS shutdown.  Waits for powered-off state."""
        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
            logger.info("  VM is already powered off.")
            return

        vm.ShutdownGuest()
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
                return
            time.sleep(5)
        raise VCenterOperationError(
            f"VM did not power off within {timeout_seconds}s"
        )

    # ------------------------------------------------------------------
    # Task helper
    # ------------------------------------------------------------------

    def _wait_for_task(
        self, task, label: str = "Task", timeout_seconds: int = 3600,
    ) -> object:
        """Poll a vCenter task to completion with progress logging."""
        start = time.monotonic()
        last_progress = -1
        while True:
            state = task.info.state
            if state == vim.TaskInfo.State.success:
                return task.info.result
            if state == vim.TaskInfo.State.error:
                raise VCenterOperationError(
                    f"{label} failed: {task.info.error.msg}"
                )
            if time.monotonic() - start > timeout_seconds:
                raise VCenterOperationError(
                    f"{label} timed out after {timeout_seconds}s"
                )
            progress = task.info.progress or 0
            if progress != last_progress:
                logger.info("  %s progress: %d%%", label, progress)
                last_progress = progress
            time.sleep(10)
