"""vCenter guest operations — run commands inside a VM."""

import logging
import time

from pyVmomi import vim

from .config import GuestConfig
from .exceptions import GuestOperationError, GuestToolsNotRunning
from .vcenter import VCenterClient

logger = logging.getLogger(__name__)

POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class GuestOperations:
    def __init__(self, vcenter_client: VCenterClient, guest_config: GuestConfig):
        self.vc = vcenter_client
        self.creds = vim.vm.guest.NamePasswordAuthentication(
            username=guest_config.user,
            password=guest_config.password,
            interactiveSession=False,
        )

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def wait_for_tools(
        self, vm: vim.VirtualMachine, timeout_seconds: int = 300,
    ) -> None:
        """Block until VMware Tools reports as running."""
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if vm.guest.toolsRunningStatus == "guestToolsRunning":
                return
            time.sleep(5)
        raise GuestToolsNotRunning(
            f"VMware Tools not running after {timeout_seconds}s"
        )

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def run_powershell(
        self,
        vm: vim.VirtualMachine,
        script_path: str,
        arguments: str = "",
        timeout_seconds: int = 600,
    ) -> int:
        """Run a PowerShell script file inside the guest.

        Args:
            script_path: Absolute path to the .ps1 file *inside the guest*.
            arguments: Additional arguments passed to the script.
            timeout_seconds: Max time to wait for completion.

        Returns:
            Process exit code.
        """
        pm = self.vc.content.guestOperationsManager.processManager
        args = f'-ExecutionPolicy Bypass -File "{script_path}"'
        if arguments:
            args += f" {arguments}"

        spec = vim.vm.guest.ProcessManager.ProgramSpec()
        spec.programPath = POWERSHELL_EXE
        spec.arguments = args

        logger.debug("  Guest exec: %s %s", POWERSHELL_EXE, args)
        pid = pm.StartProgramInGuest(vm, self.creds, spec)
        logger.info("  Started guest process PID %d", pid)
        return self._wait_for_process(vm, pid, timeout_seconds)

    def run_executable(
        self,
        vm: vim.VirtualMachine,
        exe_path: str,
        arguments: str = "",
        timeout_seconds: int = 600,
    ) -> int:
        """Run an arbitrary executable inside the guest.

        Args:
            exe_path: Absolute path to the .exe *inside the guest*.
            arguments: Command-line arguments.
            timeout_seconds: Max time to wait for completion.

        Returns:
            Process exit code.
        """
        pm = self.vc.content.guestOperationsManager.processManager

        spec = vim.vm.guest.ProcessManager.ProgramSpec()
        spec.programPath = exe_path
        spec.arguments = arguments

        logger.debug("  Guest exec: %s %s", exe_path, arguments)
        pid = pm.StartProgramInGuest(vm, self.creds, spec)
        logger.info("  Started guest process PID %d", pid)
        return self._wait_for_process(vm, pid, timeout_seconds)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _wait_for_process(
        self, vm: vim.VirtualMachine, pid: int, timeout_seconds: int,
    ) -> int:
        """Poll a guest process until it exits.  Returns exit code."""
        pm = self.vc.content.guestOperationsManager.processManager
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            procs = pm.ListProcessesInGuest(vm, self.creds, pids=[pid])
            if not procs:
                raise GuestOperationError(f"Guest process PID {pid} vanished")
            proc = procs[0]
            if proc.endTime is not None:
                logger.info("  Guest process PID %d exited with code %d", pid, proc.exitCode)
                return proc.exitCode
            time.sleep(5)
        raise GuestOperationError(
            f"Guest process PID {pid} did not finish within {timeout_seconds}s"
        )
