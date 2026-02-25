"""Ubuntu OS handler — Linux-specific migration steps."""

import base64
import logging

from .base import OSHandler
from ..exceptions import GuestOperationError, ProxmoxOperationError

logger = logging.getLogger(__name__)


class UbuntuHandler(OSHandler):

    @property
    def os_label(self) -> str:
        return "Ubuntu"

    # ------------------------------------------------------------------
    # Steps 3-5: run via VMware Tools (open-vm-tools) before shutdown
    # ------------------------------------------------------------------

    def step_3_export_nic_config(self, vm, guest_ops, config, dry_run):
        # Netplan configs already live on disk — no export needed.
        # Step 13 will replace interface names in the existing netplan files.
        logger.info("  Skipped (netplan config already on disk, will update interface names in step 13).")

    def step_4_enable_boot_driver(self, vm, guest_ops, config, dry_run):
        # VirtIO SCSI is included in the default Ubuntu initramfs — nothing to do.
        logger.info("  Skipped (VirtIO SCSI is built into the Ubuntu kernel/initramfs by default).")

    def step_5_install_virtio_tools(self, vm, guest_ops, config, dry_run):
        guest_ops.wait_for_tools(vm)
        logger.info("  open-vm-tools is running.")

        if dry_run:
            logger.info("  DRY RUN: would install qemu-guest-agent")
            return

        cmd = (
            "DEBIAN_FRONTEND=noninteractive apt-get update -q && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-guest-agent"
        )
        exit_code = guest_ops.run_sudo_bash(vm, cmd, timeout_seconds=300)
        if exit_code != 0:
            raise GuestOperationError(
                f"qemu-guest-agent installation failed with code {exit_code}"
            )
        logger.info("  qemu-guest-agent installed and enabled.")

    # ------------------------------------------------------------------
    # Steps 11-13: run via QEMU guest agent after VM is on Proxmox
    # ------------------------------------------------------------------

    def step_11_install_virtio_drivers(self, vmid, px, config, dry_run,
                                       wait_for_vm_ready, effective_wait, sleep_fn):
        # VirtIO drivers are built into the Linux kernel — nothing to install.
        logger.info("  Skipped (VirtIO drivers are built into the Linux kernel).")

    def step_12_purge_vmware_tools(self, vmid, px, config, dry_run,
                                   wait_for_vm_ready, effective_wait, sleep_fn):
        from ..migration import PRE_REBOOT_PAUSE_SECONDS, POST_REBOOT_BOOT_SECONDS

        if dry_run:
            logger.info("  DRY RUN: would remove open-vm-tools")
            return

        settle = 10 if config.migration.enable_nics_on_boot else 30
        wait_for_vm_ready(vmid, settle_seconds=settle)

        logger.info("  Waiting for QEMU guest agent ...")
        px.wait_for_guest_agent(vmid)
        logger.info("  Guest agent is responding.")

        logger.info("  Removing open-vm-tools ...")
        cmd = (
            "apt remove open-vm-tools -y; "
            "rm -rf /etc/vmware-tools; "
            "rm -f /etc/systemd/system/open-vm-tools.service; "
            "rm -f /etc/systemd/system/vmtoolsd.service; "
            "rm -rf /etc/systemd/system/open-vm-tools.service.requires; "
            "apt autoremove -y"
        )
        result = px.guest_exec(
            vmid,
            command="/bin/bash",
            arguments=["-c", cmd],
            timeout=300,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"open-vm-tools removal failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        logger.info("  open-vm-tools purged.")

        logger.info("  Waiting %ds before reboot ...", effective_wait(PRE_REBOOT_PAUSE_SECONDS))
        sleep_fn(PRE_REBOOT_PAUSE_SECONDS)
        px.reboot_vm(vmid)
        logger.info("  Waiting %ds for VM to start cleanly ...", effective_wait(POST_REBOOT_BOOT_SECONDS))
        sleep_fn(POST_REBOOT_BOOT_SECONDS)

    def step_13_restore_nic_config(self, vmid, px, config, dry_run,
                                   wait_for_vm_ready, effective_wait, sleep_fn):
        from ..migration import NIC_RESTORE_PRE_REBOOT_SECONDS, POST_REBOOT_BOOT_SECONDS

        if dry_run:
            logger.info("  DRY RUN: would update netplan interface names")
            return

        settle = 10 if config.migration.enable_nics_on_boot else 30
        wait_for_vm_ready(vmid, settle_seconds=settle)

        logger.info("  Waiting for QEMU guest agent ...")
        px.wait_for_guest_agent(vmid)
        logger.info("  Guest agent is responding.")

        # Replace old VMware interface names with new VirtIO names in netplan.
        # The script is base64-encoded to preserve newlines/indentation
        # through the QEMU guest agent API transport.
        logger.info("  Updating netplan interface names ...")
        script = """\
import json, subprocess, glob, yaml

result = subprocess.run(['ip', '-j', 'link', 'show'], capture_output=True, text=True)
links = json.loads(result.stdout)
new_ifaces = [l['ifname'] for l in sorted(links, key=lambda x: x['ifindex'])
              if l['ifname'] != 'lo']

changed = False
files = sorted(glob.glob('/etc/netplan/*.yaml') + glob.glob('/etc/netplan/*.yml'))
for f in files:
    with open(f) as fh:
        data = yaml.safe_load(fh)
    if not data or 'network' not in data:
        continue
    eths = data.get('network', {}).get('ethernets', {})
    if not eths:
        continue
    old_ifaces = sorted(eths.keys())
    new_eths = {}
    for i, old_name in enumerate(old_ifaces):
        new_name = new_ifaces[i] if i < len(new_ifaces) else old_name
        new_eths[new_name] = eths[old_name]
        if new_name != old_name:
            changed = True
    data['network']['ethernets'] = new_eths
    with open(f, 'w') as fh:
        yaml.dump(data, fh, default_flow_style=False)
print('changed' if changed else 'unchanged')
"""
        b64_script = base64.b64encode(script.encode()).decode()
        result = px.guest_exec(
            vmid,
            command="/bin/bash",
            arguments=["-c", f"echo {b64_script} | base64 -d | python3"],
            timeout=120,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"Netplan update failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        output = result.get("out-data", "").strip()
        if "changed" in output:
            logger.info("  Netplan interface names updated.")
        else:
            logger.info("  Netplan interface names already correct.")

        # Apply netplan changes
        result = px.guest_exec(
            vmid,
            command="/bin/bash",
            arguments=["-c", "netplan apply"],
            timeout=60,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"netplan apply failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        logger.info("  Netplan applied.")

        logger.info("  Waiting %ds before reboot ...", effective_wait(NIC_RESTORE_PRE_REBOOT_SECONDS))
        sleep_fn(NIC_RESTORE_PRE_REBOOT_SECONDS)
        px.reboot_vm(vmid)
        logger.info("  Waiting %ds for VM to start cleanly ...", effective_wait(POST_REBOOT_BOOT_SECONDS))
        sleep_fn(POST_REBOOT_BOOT_SECONDS)
