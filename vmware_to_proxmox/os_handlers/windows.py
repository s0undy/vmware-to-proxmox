"""Windows OS handler — existing behavior extracted from migration.py."""

import logging

from .base import OSHandler, StepContext
from ..exceptions import GuestOperationError, ProxmoxOperationError

logger = logging.getLogger(__name__)


class WindowsHandler(OSHandler):

    @property
    def os_label(self) -> str:
        return "Windows"

    def step_3_export_nic_config(self, vm, guest_ops, config, dry_run):
        guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        script = config.migration.export_nic_script
        if dry_run:
            logger.info("  DRY RUN: would run %s", script)
            return

        exit_code = guest_ops.run_powershell(vm, script)
        if exit_code != 0:
            raise GuestOperationError(
                f"exportNicConfig.ps1 exited with code {exit_code}"
            )
        logger.info("  NIC config exported to network.json inside guest.")

    def step_4_enable_boot_driver(self, vm, guest_ops, config, dry_run):
        guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        script = config.migration.vioscsi_script
        driver_path = config.migration.virtio_driver_path
        args = f'-DriverPath "{driver_path}"'

        if dry_run:
            logger.info("  DRY RUN: would run %s %s", script, args)
            return

        exit_code = guest_ops.run_powershell(vm, script, arguments=args,
                                             timeout_seconds=900)
        if exit_code != 0:
            raise GuestOperationError(
                f"enable-vioscsi-to-load-on-boot.ps1 exited with code {exit_code}"
            )
        logger.info("  VirtIO SCSI driver configured for boot loading.")

    def step_5_install_virtio_tools(self, vm, guest_ops, config, dry_run):
        guest_ops.wait_for_tools(vm)
        logger.info("  VMware Tools is running.")

        exe = config.migration.virtio_tools_path
        args = "/install /quiet /norestart"

        if dry_run:
            logger.info("  DRY RUN: would run %s %s", exe, args)
            return

        exit_code = guest_ops.run_executable(vm, exe, arguments=args,
                                             timeout_seconds=600)
        if exit_code != 0:
            raise GuestOperationError(
                f"virtio-win-guest-tools.exe exited with code {exit_code}"
            )
        logger.info("  VirtIO guest tools installed.")

    def step_11_install_virtio_drivers(self, ctx: StepContext):
        from ..migration import ISO_MOUNT_WAIT_SECONDS, VIRTIO_INSTALL_SETTLE_SECONDS

        iso_storage = ctx.config.migration.virtio_iso_storage
        iso_filename = ctx.config.migration.virtio_iso_filename

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would mount %s:iso/%s on VMID %d and install VirtIO drivers",
                         iso_storage, iso_filename, ctx.vmid)
            return

        self._wait_and_connect_agent(ctx)

        # Mount the VirtIO ISO
        ctx.px.mount_iso(ctx.vmid, iso_storage, iso_filename)
        ctx.log.info("  Waiting %ds for ISO to become available ...",
                     ctx.effective_wait(ISO_MOUNT_WAIT_SECONDS))
        ctx.sleep_fn(ISO_MOUNT_WAIT_SECONDS)

        # Discover the drive letter of the mounted ISO
        ctx.log.info("  Discovering ISO drive letter ...")
        result = ctx.px.guest_exec(
            ctx.vmid,
            command="powershell",
            arguments=["-Command",
                       "(Get-Volume | Where-Object {$_.FileSystemLabel -like 'virtio-win*'}).DriveLetter"],
            timeout=60,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"Failed to discover ISO drive letter: exit code {result['exitcode']}, "
                f"stderr: {result.get('err-data', '')}"
            )
        drive_letter = result.get("out-data", "").strip()
        if not drive_letter or len(drive_letter) != 1:
            raise ProxmoxOperationError(
                f"Unexpected drive letter result: '{drive_letter}'"
            )
        ctx.log.info("  VirtIO ISO mounted on drive %s:", drive_letter)

        # Install the full VirtIO driver package via msiexec
        msi_path = f"{drive_letter}:\\virtio-win-gt-x64.msi"
        ctx.log.info("  Installing VirtIO drivers from %s ...", msi_path)
        result = ctx.px.guest_exec(
            ctx.vmid,
            command="msiexec",
            arguments=["/i", msi_path, "/quiet", "/qn", "/norestart"],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"msiexec failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        ctx.log.info("  VirtIO driver package installed.")

        ctx.log.info("  Waiting %ds for VM to settle ...",
                     ctx.effective_wait(VIRTIO_INSTALL_SETTLE_SECONDS))
        ctx.sleep_fn(VIRTIO_INSTALL_SETTLE_SECONDS)

    def step_12_purge_vmware_tools(self, ctx: StepContext):
        from ..migration import PRE_REBOOT_PAUSE_SECONDS, POST_REBOOT_BOOT_SECONDS

        script = ctx.config.migration.purge_vmware_script

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would run %s -Force on VMID %d", script, ctx.vmid)
            return

        self._wait_and_connect_agent(ctx)

        ctx.log.info("  Running purge-vmware-tools.ps1 -Force ...")
        result = ctx.px.guest_exec(
            ctx.vmid,
            command="powershell",
            arguments=["-ExecutionPolicy", "Bypass", "-File", script, "-Force"],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"purge-vmware-tools.ps1 failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        ctx.log.info("  VMware Tools purged.")

        self._reboot_and_wait(ctx, PRE_REBOOT_PAUSE_SECONDS, POST_REBOOT_BOOT_SECONDS)

    def step_13_restore_nic_config(self, ctx: StepContext):
        from ..migration import NIC_RESTORE_PRE_REBOOT_SECONDS, POST_REBOOT_BOOT_SECONDS

        script = ctx.config.migration.import_nic_script

        if ctx.dry_run:
            ctx.log.info("  DRY RUN: would run %s on VMID %d", script, ctx.vmid)
            return

        self._wait_and_connect_agent(ctx)

        ctx.log.info("  Running importNicConfig.ps1 ...")
        result = ctx.px.guest_exec(
            ctx.vmid,
            command="powershell",
            arguments=["-ExecutionPolicy", "Bypass", "-File", script],
            timeout=600,
        )
        if result["exitcode"] != 0:
            raise ProxmoxOperationError(
                f"importNicConfig.ps1 failed with exit code {result['exitcode']}: "
                f"{result.get('err-data', '')}"
            )
        ctx.log.info("  NIC configuration restored.")

        self._reboot_and_wait(ctx, NIC_RESTORE_PRE_REBOOT_SECONDS, POST_REBOOT_BOOT_SECONDS)
