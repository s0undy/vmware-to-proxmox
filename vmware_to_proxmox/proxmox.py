"""Proxmox VE API operations."""

import logging
import math
import re
import time

import paramiko
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



class ProxmoxClient:
    def __init__(self, config: ProxmoxConfig):
        self.config = config
        self.api = None
        self._ssh = None

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
                    timeout=30,
                )
            else:
                self.api = ProxmoxAPI(
                    self.config.host,
                    user=self.config.user,
                    password=self.config.password,
                    verify_ssl=self.config.verify_ssl,
                    port=self.config.port,
                    timeout=30,
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
        cpu_type = migration_config.cpu_type
        guest_id = vm_config["guest_id"]
        if migration_config.cpu_flags:
            cpu_type = f"{cpu_type},{migration_config.cpu_flags}"

        bios = "ovmf" if vm_config["firmware"] == "efi" else "seabios"
        ostype = GUEST_ID_TO_OSTYPE.get(guest_id, "other")

        # NetApp Shift converts the disks outside Proxmox and imports the
        # finished qcow2 images in a later step, so the VM shell must be
        # created without any data disks (and without a scsi0 boot hint —
        # there is no scsi0 yet).
        skip_data_disks = (
            migration_config.disk_conversion_backend == "netapp-shift"
        )

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
            "agent": "1",
            "numa": 1,
            "tablet": 1,
            "ide2": "none,media=cdrom",
        }
        if not skip_data_disks:
            params["boot"] = "order=scsi0"

        # Disks — preserve order from vCenter
        if skip_data_disks:
            logger.info(
                "  Skipping data-disk creation (disk_conversion_backend=netapp-shift); "
                "disks will be imported after conversion.",
            )
        else:
            for i, disk in enumerate(vm_config["disks"]):
                size_gb = int(disk["size_gb"])
                if size_gb <= 0:
                    logger.warning("  Disk scsi%d: reported size is %s GB — defaulting to 1 GB", i, disk["size_gb"])
                    size_gb = 1
                params[f"scsi{i}"] = f"{storage}:{size_gb},format=vmdk,ssd=1,discard=on,iothread=1"

        # NICs — preserve order, use ordered bridge list
        bridges = [b.strip() for b in migration_config.proxmox_bridges.split(",")]
        for i, nic in enumerate(vm_config["nics"]):
            bridge = bridges[min(i, len(bridges) - 1)]
            if i >= len(bridges):
                logger.warning("  NIC net%d: reusing bridge %s (no bridge specified for this NIC)", i, bridge)
            if migration_config.enable_nics_on_boot:
                params[f"net{i}"] = f"virtio,bridge={bridge}"
            else:
                params[f"net{i}"] = f"virtio,bridge={bridge},link_down=1"

        # EFI disk when using OVMF. For netapp-shift there is no later
        # move step, so place the NVRAM disk directly on the final storage.
        if bios == "ovmf":
            efi_storage = (
                migration_config.proxmox_final_storage
                if skip_data_disks and migration_config.proxmox_final_storage
                else storage
            )
            params["efidisk0"] = f"{efi_storage}:1,format=qcow2,efitype=4m,pre-enrolled-keys=1"

        try:
            self.api.nodes(node).qemu.create(**params)
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to create VM on Proxmox: {exc}"
            ) from exc

        logger.info("  Created Proxmox VM — VMID: %d", vmid)
        return vmid

    # ------------------------------------------------------------------
    # VM lifecycle & disk operations
    # ------------------------------------------------------------------

    def start_vm(self, vmid: int) -> None:
        """Start a Proxmox VM."""
        node = self.config.node
        try:
            self.api.nodes(node).qemu(vmid).status.start.post()
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to start VM {vmid}: {exc}"
            ) from exc
        logger.info("  Start command sent for VMID %d", vmid)

    def wait_for_task(self, task_upid: str, timeout: int = 3600) -> None:
        """Poll a Proxmox task until it completes or times out."""
        node = self.config.node
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ProxmoxOperationError(
                    f"Task timed out after {timeout}s: {task_upid}"
                )
            status = self.api.nodes(node).tasks(task_upid).status.get()
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise ProxmoxOperationError(
                        f"Task failed ({status.get('exitstatus')}): {task_upid}"
                    )
                return
            time.sleep(3)

    def move_disk(self, vmid: int, disk: str, target_storage: str,
                  timeout: int = 3600) -> None:
        """Move a VM disk to another storage, converting to qcow2.

        The source disk is kept (delete=0).
        """
        node = self.config.node
        logger.info("    Moving %s -> %s (qcow2) ...", disk, target_storage)
        try:
            upid = self.api.nodes(node).qemu(vmid).move_disk.post(
                disk=disk,
                storage=target_storage,
                format="qcow2",
                delete=0,
            )
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to move disk {disk} for VM {vmid}: {exc}"
            ) from exc
        self.wait_for_task(upid, timeout=timeout)
        logger.info("    %s move complete.", disk)

    def reboot_vm(self, vmid: int) -> None:
        """Reboot a Proxmox VM via ACPI signal."""
        node = self.config.node
        try:
            self.api.nodes(node).qemu(vmid).status.reboot.post()
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to reboot VM {vmid}: {exc}"
            ) from exc
        logger.info("  Reboot command sent for VMID %d", vmid)

    def add_to_ha(self, vmid: int) -> None:
        """Add a VM to the Proxmox HA manager."""
        try:
            self.api.cluster.ha.resources.create(sid=f"vm:{vmid}")
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to add VM {vmid} to HA: {exc}"
            ) from exc
        logger.info("  VM %d added to HA.", vmid)

    def mount_iso(self, vmid: int, storage: str, iso_filename: str) -> None:
        """Mount an ISO image on the VM's IDE CD/DVD drive."""
        node = self.config.node
        ide2_value = f"{storage}:iso/{iso_filename},media=cdrom"
        try:
            self.api.nodes(node).qemu(vmid).config.put(ide2=ide2_value)
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to mount ISO on VM {vmid}: {exc}"
            ) from exc
        logger.info("  Mounted ISO: %s", ide2_value)

    def unmount_iso(self, vmid: int) -> None:
        """Unmount the ISO from the VM's IDE CD/DVD drive (keep the drive)."""
        node = self.config.node
        try:
            self.api.nodes(node).qemu(vmid).config.put(ide2="none,media=cdrom")
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to unmount ISO on VM {vmid}: {exc}"
            ) from exc
        logger.info("  ISO unmounted (ide2 reset to empty).")

    def wait_for_guest_agent(self, vmid: int, timeout: int = 300) -> None:
        """Block until the QEMU guest agent responds to a ping."""
        node = self.config.node
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ProxmoxOperationError(
                    f"QEMU guest agent not responding after {timeout}s for VM {vmid}"
                )
            try:
                self.api.nodes(node).qemu(vmid).agent.ping.post()
                return
            except Exception:
                time.sleep(5)

    def guest_exec(
        self,
        vmid: int,
        command: str,
        arguments: list[str] | None = None,
        timeout: int = 600,
    ) -> dict:
        """Execute a command inside the guest via QEMU guest agent.

        Args:
            vmid: Proxmox VM ID.
            command: Path to the executable inside the guest.
            arguments: List of command-line arguments.
            timeout: Max seconds to wait for completion.

        Returns:
            Dict with 'exitcode', 'out-data', 'err-data'.
        """
        node = self.config.node
        # Proxmox 8+ expects command as a list: [executable, arg1, arg2, ...]
        cmd_list = [command] + (arguments or [])

        try:
            result = self.api.nodes(node).qemu(vmid).agent("exec").post(command=cmd_list)
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to execute command in guest VM {vmid}: {exc}"
            ) from exc

        pid = result.get("pid")
        if pid is None:
            raise ProxmoxOperationError(
                f"No PID returned from guest exec on VM {vmid}"
            )

        logger.info("  Guest exec PID %s: %s", pid, command)

        # Poll for completion
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ProxmoxOperationError(
                    f"Guest process PID {pid} did not finish within {timeout}s on VM {vmid}"
                )
            try:
                status = self.api.nodes(node).qemu(vmid).agent("exec-status").get(pid=pid)
            except Exception:
                time.sleep(5)
                continue

            if status.get("exited"):
                exitcode = status.get("exitcode", -1)
                out_data = status.get("out-data", "")
                err_data = status.get("err-data", "")
                logger.info("  Guest exec PID %s exited with code %d", pid, exitcode)
                return {"exitcode": exitcode, "out-data": out_data, "err-data": err_data}
            time.sleep(5)

    def set_nic_link_state(self, vmid: int, nic_key: str, link_down: bool) -> None:
        """Update the link state of a VM NIC.

        Args:
            vmid: Proxmox VM ID.
            nic_key: NIC config key (e.g. 'net0').
            link_down: True to disable link, False to enable.
        """
        node = self.config.node
        config = self.get_vm_config_proxmox(vmid)
        current_value = config.get(nic_key, "")
        if not current_value:
            logger.warning("  NIC %s not found in VM %d config, skipping.", nic_key, vmid)
            return

        if link_down:
            new_value = re.sub(r"link_down=\d", "link_down=1", current_value)
            if "link_down=" not in new_value:
                new_value += ",link_down=1"
        else:
            new_value = re.sub(r",?link_down=\d", "", current_value)

        try:
            self.api.nodes(node).qemu(vmid).config.put(**{nic_key: new_value})
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to update {nic_key} link state on VM {vmid}: {exc}"
            ) from exc
        logger.info("  %s link_down=%s -> %s", nic_key, link_down, new_value)

    def get_vm_config_proxmox(self, vmid: int) -> dict:
        """Return the current Proxmox VM configuration."""
        node = self.config.node
        return self.api.nodes(node).qemu(vmid).config.get()

    def delete_unused_disks(self, vmid: int) -> None:
        """Delete all unused (detached) disks from a Proxmox VM."""
        node = self.config.node
        config = self.get_vm_config_proxmox(vmid)
        unused_keys = sorted(
            k for k in config if k.startswith("unused") and k[6:].isdigit()
        )
        if not unused_keys:
            logger.info("  No unused disks found.")
            return

        logger.info("  Found %d unused disk(s): %s", len(unused_keys), ", ".join(unused_keys))
        try:
            self.api.nodes(node).qemu(vmid).config.put(delete=",".join(unused_keys))
        except Exception as exc:
            raise ProxmoxOperationError(
                f"Failed to delete unused disks on VM {vmid}: {exc}"
            ) from exc
        logger.info("  Unused disks deleted.")

    def get_vm_status(self, vmid: int) -> str:
        """Return the current power status of a Proxmox VM (e.g. 'running')."""
        node = self.config.node
        data = self.api.nodes(node).qemu(vmid).status.current.get()
        return data.get("status", "unknown")

    def get_guest_ip(self, vmid: int) -> str | None:
        """Return the primary IPv4 address reported by the QEMU guest agent.

        Returns None if the guest agent is unavailable or no suitable
        address is found.
        """
        node = self.config.node
        try:
            result = self.api.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
        except Exception:
            return None

        interfaces = result.get("result", result) if isinstance(result, dict) else result
        if not isinstance(interfaces, list):
            return None

        for iface in interfaces:
            name = iface.get("name", "")
            if name == "lo" or name.startswith("Loopback"):
                continue
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    ip = addr.get("ip-address", "")
                    if ip and not ip.startswith("127."):
                        return ip
        return None

    # ------------------------------------------------------------------
    # SSH transport (for file operations on the Proxmox node)
    # ------------------------------------------------------------------

    def _get_ssh(self) -> paramiko.SSHClient:
        """Return a cached SSH connection to the Proxmox node."""
        if self._ssh is not None:
            return self._ssh
        ssh_user = self.config.ssh_user or self.config.user.split("@")[0]
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._ssh.connect(
                hostname=self.config.host,
                port=self.config.ssh_port,
                username=ssh_user,
                password=self.config.password,
            )
        except Exception as exc:
            self._ssh = None
            raise ProxmoxConnectionError(
                f"SSH connection to {self.config.host} failed: {exc}"
            ) from exc
        return self._ssh

    def _ssh_read_file(self, path: str) -> str:
        """Read a text file from the Proxmox node via SFTP."""
        ssh = self._get_ssh()
        sftp = ssh.open_sftp()
        try:
            with sftp.open(path, "r") as f:
                return f.read().decode()
        finally:
            sftp.close()

    def _ssh_write_file(self, path: str, content: str) -> None:
        """Write a text file on the Proxmox node via SFTP."""
        ssh = self._get_ssh()
        sftp = ssh.open_sftp()
        try:
            with sftp.open(path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def close_ssh(self):
        """Close the SSH connection if open."""
        if self._ssh is not None:
            self._ssh.close()
            self._ssh = None

    def close(self):
        """Close all connections (SSH and API)."""
        self.close_ssh()
        self.api = None

    # ------------------------------------------------------------------
    # Storage path resolution
    # ------------------------------------------------------------------

    def get_storage_path(self, storage_name: str) -> str:
        """Return the local mount path for a Proxmox storage pool."""
        try:
            storage_cfg = self.api.storage(storage_name).get()
            path = storage_cfg.get("path")
            if path:
                return path
        except Exception:
            pass
        return f"/mnt/pve/{storage_name}"

    # ------------------------------------------------------------------
    # VMDK descriptor rewriting
    # ------------------------------------------------------------------

    _EXTENT_RE = re.compile(
        r'^((?:RW|RDONLY|NOACCESS)\s+\d+\s+'
        r'(?:VMFS|FLAT|SPARSE|ZERO|VMFSSPARSE)\s+")'
        r'([^"]+)'
        r'(")',
        re.MULTILINE,
    )

    def rewrite_vmdk_descriptors(
        self,
        vmid: int,
        vm_config: dict,
        storage_name: str,
    ) -> None:
        """Copy VMware VMDK descriptors to Proxmox disk locations and rewrite extent paths.

        For each disk, the VMware descriptor is read from the shared NFS storage,
        its extent line is rewritten to use a relative path back to the original
        flat file, and the result is written over the empty Proxmox descriptor.
        """
        storage_path = self.get_storage_path(storage_name)
        proxmox_images_dir = f"{storage_path}/images/{vmid}"

        try:
            for i, disk in enumerate(vm_config["disks"]):
                vmware_filename = disk["filename"]  # e.g. "[datastore] VM1/VM1.vmdk"

                # Parse "[datastore] path/to/file.vmdk" → "path/to/file.vmdk"
                match = re.match(r"\[.*?]\s*(.+)", vmware_filename)
                if not match:
                    raise ProxmoxOperationError(
                        f"Cannot parse VMware disk filename: {vmware_filename}"
                    )
                relative_path = match.group(1)  # e.g. "VM1/VM1.vmdk"

                parts = relative_path.split("/")
                if len(parts) >= 2:
                    vm_folder = "/".join(parts[:-1])
                else:
                    vm_folder = ""

                # Source: VMware descriptor on shared NFS
                vmware_descriptor_path = f"{storage_path}/{relative_path}"

                # Destination: Proxmox descriptor
                proxmox_disk_name = f"vm-{vmid}-disk-{i}.vmdk"
                proxmox_descriptor_path = f"{proxmox_images_dir}/{proxmox_disk_name}"

                logger.info("  Disk scsi%d: %s -> %s", i, vmware_filename, proxmox_disk_name)

                # Read the VMware descriptor
                descriptor_content = self._ssh_read_file(vmware_descriptor_path)

                # Validate it looks like a descriptor (not a flat file)
                if len(descriptor_content) > 10_000:
                    raise ProxmoxOperationError(
                        f"File looks too large to be a VMDK descriptor ({len(descriptor_content)} bytes): "
                        f"{vmware_descriptor_path}"
                    )

                # Rewrite extent lines to use relative paths
                extent_match = self._EXTENT_RE.search(descriptor_content)
                if not extent_match:
                    raise ProxmoxOperationError(
                        f"Cannot find extent line in VMDK descriptor: {vmware_descriptor_path}"
                    )

                def _rewrite_extent(m):
                    original_flat_name = m.group(2)
                    if vm_folder:
                        new_ref = f"../../{vm_folder}/{original_flat_name}"
                    else:
                        new_ref = f"../../{original_flat_name}"
                    return m.group(1) + new_ref + m.group(3)

                new_descriptor = self._EXTENT_RE.sub(_rewrite_extent, descriptor_content)

                # Write the modified descriptor to the Proxmox location
                self._ssh_write_file(proxmox_descriptor_path, new_descriptor)

                logger.info("    Extent rewritten: %s", extent_match.group(2))
        finally:
            self.close_ssh()
