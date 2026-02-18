"""Proxmox VE API operations."""

import logging
import math

from proxmoxer import ProxmoxAPI

from .config import MigrationConfig, ProxmoxConfig
from .exceptions import ProxmoxConnectionError, ProxmoxOperationError

logger = logging.getLogger(__name__)

# Map VMware guestId values to Proxmox ostype values.
# Valid Proxmox ostypes: other, wxp, w2k, w2k3, w2k8, wvista, win7, win8, win10, win11,
#                        l24, l26, solaris
GUEST_ID_TO_OSTYPE = {
    # Windows Server
    "windows9Server64Guest": "win10",          # Server 2016
    "windows2019srv_64Guest": "win10",         # Server 2019
    "windows2019srvNext_64Guest": "win10",     # Server 2022
    "windows2022srvNext_64Guest": "win11",     # Server 2025 (vNext of 2022)
    "windows2025srv_64Guest": "win11",         # Server 2025
    "windows2025srvNext_64Guest": "win11",     # Server 2025
    # Windows Desktop
    "windows9_64Guest": "win10",
    "windows11_64Guest": "win11",
    "windows12_64Guest": "win11",
    # Linux
    "ubuntu64Guest": "l26",
    "rhel8_64Guest": "l26",
    "rhel9_64Guest": "l26",
    "centos8_64Guest": "l26",
    "debian10_64Guest": "l26",
    "debian11_64Guest": "l26",
    "debian12_64Guest": "l26",
}

# Guest IDs for which nested virtualisation must be explicitly disabled.
NO_NESTED_VIRT_GUEST_IDS = {
    "windows11_64Guest",
    "windows12_64Guest",
    "windows2022srvNext_64Guest",
    "windows2025srv_64Guest",
    "windows2025srvNext_64Guest",
}


class ProxmoxClient:
    def __init__(self, config: ProxmoxConfig):
        self.config = config
        self.api = None

    def connect(self):
        """Establish a connection to the Proxmox API."""
        try:
            if self.config.token_name and self.config.token_value:
                self.api = ProxmoxAPI(
                    self.config.host,
                    user=self.config.user,
                    token_name=self.config.token_name,
                    token_value=self.config.token_value,
                    verify_ssl=self.config.verify_ssl,
                    port=self.config.port,
                )
            else:
                self.api = ProxmoxAPI(
                    self.config.host,
                    user=self.config.user,
                    password=self.config.password,
                    verify_ssl=self.config.verify_ssl,
                    port=self.config.port,
                )
            # Quick connectivity check
            self.api.version.get()
        except Exception as exc:
            raise ProxmoxConnectionError(
                f"Failed to connect to Proxmox {self.config.host}: {exc}"
            ) from exc

    def get_next_vmid(self) -> int:
        """Return the next available VMID from the cluster."""
        return int(self.api.cluster.nextid.get())

    def create_vm(
        self,
        vm_config: dict,
        migration_config: MigrationConfig,
    ) -> int:
        """Create a Proxmox VM shell that mirrors the vCenter VM.

        Disks use VMDK format and are created in the same order as they
        appear in vCenter.  NICs are likewise created in order.

        Args:
            vm_config: Plain dict produced by VCenterClient.get_vm_config().
            migration_config: Migration settings (storage, bridges, limits).

        Returns:
            The VMID of the newly created VM.
        """
        vmid = migration_config.proxmox_vmid or self.get_next_vmid()
        node = self.config.node
        storage = migration_config.proxmox_storage

        # CPU topology --------------------------------------------------
        total_cpus = vm_config["num_cpus"]
        max_cores = migration_config.max_cores
        max_sockets = migration_config.max_sockets

        if max_cores > 0 and total_cpus > max_cores:
            sockets = min(math.ceil(total_cpus / max_cores), max_sockets)
            cores = math.ceil(total_cpus / sockets)
        else:
            sockets = 1
            cores = total_cpus

        # CPU type ------------------------------------------------------
        guest_id = vm_config["guest_id"]
        if guest_id in NO_NESTED_VIRT_GUEST_IDS:
            cpu_type = "host,-vmx"
        else:
            cpu_type = "host"

        bios = "ovmf" if vm_config["firmware"] == "efi" else "seabios"
        ostype = GUEST_ID_TO_OSTYPE.get(guest_id, "other")

        params = {
            "vmid": vmid,
            "name": vm_config["name"],
            "machine": "q35",
            "memory": vm_config["memory_mb"],
            "sockets": sockets,
            "cores": cores,
            "cpu": cpu_type,
            "bios": bios,
            "ostype": ostype,
            "scsihw": "virtio-scsi-single",
            "boot": "order=scsi0",
            "agent": "1",
            "numa": 1,
            "tablet": 0,
            "ide0": "none,media=cdrom",
        }

        # Disks — preserve order from vCenter
        for i, disk in enumerate(vm_config["disks"]):
            size_gb = int(disk["size_gb"]) or 1
            params[f"scsi{i}"] = f"{storage}:{size_gb},format=vmdk"

        # NICs — preserve order, use ordered bridge list
        bridges = [b.strip() for b in migration_config.proxmox_bridges.split(",")]
        for i, nic in enumerate(vm_config["nics"]):
            bridge = bridges[min(i, len(bridges) - 1)]
            params[f"net{i}"] = f"virtio,bridge={bridge}"

        # EFI disk when using OVMF
        if bios == "ovmf":
            params["efidisk0"] = f"{storage}:1,format=qcow2,efitype=4m,pre-enrolled-keys=1"

        try:
            self.api.nodes(node).qemu.create(**params)
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to create VM on Proxmox: {exc}"
            ) from exc

        logger.info("  Created Proxmox VM — VMID: %d", vmid)
        return vmid
