"""NetApp Shift backend — foundation stub.

The REST client (NetAppShiftClient) is the real foundation. This backend
authenticates on prepare() so connection issues surface early, and leaves
each step raising NotImplementedError until the follow-up pass wires the
client's disk-operation methods to the migration flow.
"""

from ..config import NetAppShiftConfig
from ..netapp_shift import NetAppShiftClient
from .base import BackendContext, DiskMigrationBackend


class NetAppShiftBackend(DiskMigrationBackend):
    name = "netapp-shift"

    def __init__(self, shift_config: NetAppShiftConfig):
        self.shift_config = shift_config
        self.client: NetAppShiftClient | None = None

    def prepare(self, ctx: BackendContext) -> None:
        ctx.log.info("Initializing NetApp Shift backend ...")
        self.client = NetAppShiftClient(self.shift_config)
        self.client.connect()
        ctx.log.info("  NetApp Shift backend ready.")

    def step_6_shutdown(self, ctx: BackendContext, vm) -> None:
        raise NotImplementedError("NetApp Shift backend: step 6 not yet implemented")

    def step_7_rewrite_vmdk_descriptors(self, ctx: BackendContext, vm) -> None:
        raise NotImplementedError("NetApp Shift backend: step 7 not yet implemented")

    def step_8_start_vm(self, ctx: BackendContext, vm) -> None:
        raise NotImplementedError("NetApp Shift backend: step 8 not yet implemented")

    def step_9_move_disks(self, ctx: BackendContext, vm) -> None:
        raise NotImplementedError("NetApp Shift backend: step 9 not yet implemented")

    def step_10_verify(self, ctx: BackendContext, vm) -> None:
        raise NotImplementedError("NetApp Shift backend: step 10 not yet implemented")
