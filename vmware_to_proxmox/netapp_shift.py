"""NetApp Shift REST API client.

Talks to the multi-port NetApp Shift API surface:

  - :3698 — tenant sessions (login/logout)
  - :3700 — setup APIs (sites, VMs, resource groups, blueprints)
  - :3704 — recovery APIs (convert execution + execution step polling)

Authentication uses POST /api/tenant/session, then propagates the returned
session id via the ``netapp-sie-sessionid`` header on every subsequent call.
Payload shapes are ported from the reference Python implementation at
https://github.com/NetApp/shift-api-automation/tree/main/Python.
"""

import logging
import time
import urllib3

import requests

from .config import NetAppShiftConfig
from .exceptions import NetAppShiftConnectionError, NetAppShiftError

logger = logging.getLogger(__name__)

SESSION_PORT = 3698
SETUP_PORT = 3700
RECOVERY_PORT = 3704

DEFAULT_TIMEOUT = 60

LOGIN_PATH = "/api/tenant/session"
LOGOUT_PATH = "/api/tenant/session/end"

SESSION_HEADER = "netapp-sie-sessionid"


class NetAppShiftClient:
    """Thin REST client for the NetApp Shift API.

    Usage:
        client = NetAppShiftClient(config)
        client.connect()
        rg_id = client.create_resource_group(...)
        bp_id = client.create_blueprint(...)
        execution_id = client.trigger_conversion(bp_id)
        client.wait_for_execution(execution_id)
        client.close()
    """

    def __init__(self, config: NetAppShiftConfig):
        self.config = config
        self.session = requests.Session()
        self.session.verify = config.verify_ssl
        self._session_id: str | None = None

        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------
    # Connection / dispatch
    # ------------------------------------------------------------------

    def _url(self, port: int, path: str) -> str:
        return f"https://{self.config.host}:{port}{path}"

    def connect(self) -> None:
        """Authenticate and store the session id."""
        url = self._url(SESSION_PORT, LOGIN_PATH)
        logger.info("Connecting to NetApp Shift at %s ...", url)
        try:
            response = self.session.post(
                url,
                json={"loginId": self.config.user, "password": self.config.password},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise NetAppShiftConnectionError(
                f"Failed to reach NetApp Shift at {url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise NetAppShiftConnectionError(
                f"NetApp Shift login failed ({response.status_code}): {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise NetAppShiftConnectionError(
                f"NetApp Shift login returned non-JSON body: {exc}"
            ) from exc

        session_obj = payload.get("session") or {}
        session_id = session_obj.get("_id")
        if not session_id:
            raise NetAppShiftConnectionError(
                "NetApp Shift login response did not contain session._id"
            )

        self._session_id = session_id
        self.session.headers[SESSION_HEADER] = session_id
        logger.info("  NetApp Shift: authenticated as %s", self.config.user)

    def close(self) -> None:
        """Best-effort logout. Safe to call multiple times."""
        if self._session_id is None:
            return
        url = self._url(SESSION_PORT, LOGOUT_PATH)
        try:
            self.session.post(
                url,
                json={"sessionId": self._session_id},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException:
            pass
        self._session_id = None
        self.session.headers.pop(SESSION_HEADER, None)

    def _request(self, method: str, port: int, path: str, **kwargs) -> dict:
        url = self._url(port, path)
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise NetAppShiftError(
                f"NetApp Shift request failed ({method} {url}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise NetAppShiftError(
                f"NetApp Shift {method} {url} returned {response.status_code}: "
                f"{response.text}"
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise NetAppShiftError(
                f"NetApp Shift {method} {url} returned non-JSON body: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    def get_site_id_by_name(self, name: str) -> str:
        payload = self._request("GET", SETUP_PORT, "/api/setup/site")
        for site in payload.get("list", []) or []:
            if site.get("name") == name:
                site_id = site.get("_id")
                if not site_id:
                    raise NetAppShiftError(
                        f"NetApp Shift site '{name}' is missing _id"
                    )
                return site_id
        raise NetAppShiftError(f"NetApp Shift site not found: {name}")

    def get_virt_env_id(self, site_id: str) -> str:
        payload = self._request("GET", SETUP_PORT, f"/api/setup/site/{site_id}")
        envs = payload.get("virtualizationEnvironments") or []
        if not envs or not envs[0].get("_id"):
            raise NetAppShiftError(
                f"NetApp Shift site {site_id} has no virtualizationEnvironments"
            )
        return envs[0]["_id"]

    def discover_source(self, site_id: str, virt_env_id: str) -> None:
        """Kick off a source-site discovery.

        Fire-and-forget: NetApp Shift accepts an empty POST and starts
        refreshing its inventory of the source virtualization environment.
        Callers should wait a few seconds before reading the unprotected
        VM list so that the refresh has time to settle.
        """
        self._request(
            "POST",
            SETUP_PORT,
            "/api/setup/source/discovery",
            params={"siteId": site_id, "virtEnvId": virt_env_id},
        )

    def get_unprotected_vm_by_name(
        self, site_id: str, virt_env_id: str, vm_name: str,
    ) -> dict:
        """Return the full VM dict for an unprotected VM, by name."""
        payload = self._request(
            "GET",
            SETUP_PORT,
            "/api/setup/vm/unprotected",
            params={"siteId": site_id, "virtEnvId": virt_env_id},
        )
        for vm in payload.get("list", []) or []:
            if vm.get("name") == vm_name:
                if not vm.get("_id"):
                    raise NetAppShiftError(
                        f"NetApp Shift VM '{vm_name}' is missing _id"
                    )
                return vm
        raise NetAppShiftError(
            f"NetApp Shift unprotected VM not found: {vm_name} "
            f"(site={site_id}, virtEnv={virt_env_id})"
        )

    # ------------------------------------------------------------------
    # Resource group / blueprint creation
    # ------------------------------------------------------------------

    def create_resource_group(
        self,
        *,
        name: str,
        source_site_id: str,
        source_virt_env_id: str,
        dest_site_id: str,
        dest_virt_env_id: str,
        vm_id: str,
        vm_name: str,
        datastore_name: str,
        volume_name: str,
        qtree_name: str,
        boot_order: int = 3,
    ) -> str:
        body = {
            "name": name,
            "sourceSite": {"_id": source_site_id},
            "sourceVirtEnv": {"_id": source_virt_env_id},
            "vms": [{"_id": vm_id, "order": boot_order}],
            "bootOrder": {
                "vms": [{"vm": {"_id": vm_id}, "order": boot_order}],
            },
            "bootDelay": [
                {"vm": {"_id": vm_id}, "delaySecs": 0},
            ],
            "scripts": [],
            "replicationPlan": {
                "targetSite": {"_id": dest_site_id},
                "targetVirtEnv": {"_id": dest_virt_env_id},
                "datastoreQtreeMapping": [
                    {
                        "datastoreName": datastore_name,
                        "qtreeName": qtree_name,
                        "volumeName": volume_name,
                        "volumePath": "",
                    }
                ],
                "targetDatastore": {"_id": datastore_name},
                "snapshotType": "clone_based_conversion",
                "frequencyMins": "30",
                "retryCount": 3,
                "numSnapshotsToRetain": 2,
            },
            "migrationMode": "clone_based_conversion",
            "singleDatastoreForOpenShift": None,
        }
        payload = self._request(
            "POST", SETUP_PORT, "/api/setup/protectionGroup", json=body,
        )
        rg_id = payload.get("_id") or (payload.get("protectionGroup") or {}).get("_id")
        if not rg_id:
            raise NetAppShiftError(
                f"NetApp Shift create resource group response missing _id: {payload}"
            )
        return rg_id

    def create_blueprint(
        self,
        *,
        name: str,
        source_site_id: str,
        source_virt_env_id: str,
        dest_site_id: str,
        dest_virt_env_id: str,
        resource_group_id: str,
        vm_info: dict,
        boot_order: int = 3,
    ) -> str:
        vm_id = vm_info.get("_id")
        if not vm_id:
            raise NetAppShiftError("create_blueprint: vm_info is missing _id")

        network_details = (
            vm_info.get("networkDetails")
            or vm_info.get("networks")
            or []
        )
        network_names = [n.get("name", "") for n in network_details if n.get("name")]

        body = {
            "name": name,
            "sourceSite": {"_id": source_site_id},
            "sourceVirtEnv": {"_id": source_virt_env_id},
            "targetSite": {"_id": dest_site_id},
            "targetVirtEnv": {"_id": dest_virt_env_id},
            "rpoSeconds": 0,
            "rtoSeconds": 0,
            "protectionGroups": [{"_id": resource_group_id}],
            "bootOrder": {
                "protectionGroups": [
                    {
                        "protectionGroup": {"_id": resource_group_id},
                        "order": boot_order,
                    },
                ],
                "vms": [{"vm": {"_id": vm_id}, "order": boot_order}],
            },
            "vmSettings": [
                {
                    "vm": {"_id": vm_id},
                    "name": vm_info.get("name", ""),
                    "numCPUs": vm_info.get("numCPUs", 0),
                    "memoryMB": vm_info.get("memoryMB", 0),
                    "ip": "",
                    "vmGeneration": vm_info.get("vmGeneration", "1"),
                    "nicIp": vm_info.get("nicIp", []),
                    "isSecureBootEnable": bool(
                        vm_info.get("isSecureBootEnable", False)
                    ),
                    "retainMacAddress": False,
                    "removeVMwareTools": False,
                    "networkDetails": network_details,
                    "networkName": network_names,
                    "order": boot_order,
                    "ipAllocType": "dynamic",
                    "powerOnFlag": True,
                    "serviceAccountOverrideFlag": False,
                    "serviceAccount": {"loginId": "", "password": ""},
                }
            ],
            "mappings": [],
            "scheduledDateTime": None,
            "ipConfig": {"type": "do_not_config", "targetNetworks": []},
            "serviceAccounts": [],
            "overridePrepareVM": True,
        }
        payload = self._request(
            "POST", SETUP_PORT, "/api/setup/drplan", json=body,
        )
        bp_id = payload.get("_id") or (payload.get("drPlan") or {}).get("_id")
        if not bp_id:
            raise NetAppShiftError(
                f"NetApp Shift create blueprint response missing _id: {payload}"
            )
        return bp_id

    def get_resource_group_by_name(self, name: str) -> dict | None:
        payload = self._request("GET", SETUP_PORT, "/api/setup/protectionGroup")
        for rg in payload.get("list", []) or []:
            if rg.get("name") == name:
                return rg
        return None

    def get_resource_group_id_by_name(self, name: str) -> str | None:
        rg = self.get_resource_group_by_name(name)
        return rg.get("_id") if rg else None

    def get_resource_group_detail(self, rg_id: str) -> dict:
        """Return the full resource group object by id."""
        payload = self._request(
            "GET", SETUP_PORT, f"/api/setup/protectionGroup/{rg_id}",
        )
        return payload.get("protectionGroup") or payload or {}

    def get_resource_group_vm_info(self, rg_name: str) -> dict | None:
        """Best-effort resume helper: return the first VM dict stored on a RG.

        Note: whether the RG response carries the full per-VM fields
        (numCPUs, networkDetails, ...) depends on the NetApp Shift backend.
        If fields are missing, the blueprint payload will be sparse — in that
        case, re-run from step 7 while the VM is still unprotected.
        """
        rg = self.get_resource_group_by_name(rg_name)
        if not rg or not rg.get("_id"):
            return None
        detail = self.get_resource_group_detail(rg["_id"])
        vms = detail.get("vms") or rg.get("vms") or []
        if vms and isinstance(vms[0], dict):
            return vms[0]
        return None

    def get_blueprint_id_by_name(self, name: str) -> str | None:
        payload = self._request("GET", SETUP_PORT, "/api/setup/drplan")
        for bp in payload.get("list", []) or []:
            if bp.get("name") == name:
                return bp.get("_id")
        return None

    # ------------------------------------------------------------------
    # Conversion execution + status
    # ------------------------------------------------------------------
    #
    # NetApp Shift execution step status codes:
    #   0/1 = pending/queued, 2 = running, 3 = in progress, 4 = success.
    # We require EVERY step to reach status=4 before treating the
    # execution as complete — otherwise a multi-disk conversion can
    # exit early while one disk is still at status=3.
    STEP_STATUS_SUCCESS = 4

    def trigger_conversion(self, blueprint_id: str) -> str:
        """POST /api/recovery/bluePrint/{id}/convert/execution.

        Returns the execution _id that can be polled via
        /api/recovery/execution/{execution_id}/steps.
        """
        payload = self._request(
            "POST",
            RECOVERY_PORT,
            f"/api/recovery/bluePrint/{blueprint_id}/convert/execution",
        )
        execution_id = payload.get("_id")
        if not execution_id:
            raise NetAppShiftError(
                f"NetApp Shift trigger conversion response missing _id: {payload}"
            )
        return execution_id

    def get_execution_steps(self, execution_id: str) -> tuple[str, list[dict]]:
        """Return (job_type, steps) for a running or finished execution."""
        payload = self._request(
            "GET",
            RECOVERY_PORT,
            f"/api/recovery/execution/{execution_id}/steps",
        )
        return payload.get("type", ""), payload.get("steps") or []

    # Number of consecutive "all status=4 with stable step count" polls
    # required before we treat the execution as complete. Defends against
    # the steps list growing mid-execution: e.g. disk 1's per-disk step
    # might already be at status=4 in poll N while disk 2's per-disk step
    # has not yet been added to the list — declaring done at that point
    # would race the orchestrator into step 10 against an in-flight
    # conversion. Requiring N consecutive stable polls forces NetApp Shift
    # to publish a steady-state list before we trust it.
    EXECUTION_STABLE_POLLS = 3

    def wait_for_execution(
        self,
        execution_id: str,
        *,
        poll_interval: int = 30,
        timeout: int = 14400,
    ) -> None:
        """Poll execution steps until every step has reached status=4
        and the step list has been stable across several consecutive polls.

        A step is:
          - *failed* if it carries a truthy ``error`` field;
          - *done* only if its ``status`` equals ``STEP_STATUS_SUCCESS`` (4);
          - otherwise *still pending* — we keep polling.

        We additionally require the step count to remain unchanged for
        ``EXECUTION_STABLE_POLLS`` consecutive polls of "all done" before
        returning, because NetApp Shift adds per-disk steps to the list
        as the conversion progresses; a poll that catches the list
        mid-grow would otherwise let us bail too early on multi-disk VMs.
        """
        deadline = time.monotonic() + timeout
        last_desc = ""
        stable_polls = 0
        last_count = -1
        while True:
            _, steps = self.get_execution_steps(execution_id)

            failed = [s for s in steps if s.get("error")]
            if failed:
                details = "; ".join(
                    f"{s.get('description', '?')} "
                    f"(status={s.get('status')}, error={s.get('error')})"
                    for s in failed
                )
                raise NetAppShiftError(
                    f"NetApp Shift execution {execution_id} failed: {details}"
                )

            count = len(steps)
            all_done = bool(steps) and all(
                s.get("status") == self.STEP_STATUS_SUCCESS for s in steps
            )

            if all_done and count == last_count:
                stable_polls += 1
                logger.info(
                    "  NetApp Shift execution %s: all %d steps at status=4 "
                    "(stable %d/%d)",
                    execution_id, count, stable_polls, self.EXECUTION_STABLE_POLLS,
                )
                if stable_polls >= self.EXECUTION_STABLE_POLLS:
                    logger.info(
                        "  NetApp Shift execution %s completed "
                        "(%d steps, all status=4).",
                        execution_id, count,
                    )
                    return
            else:
                if all_done:
                    logger.info(
                        "  NetApp Shift execution %s: all %d steps at status=4 "
                        "(step count changed %d -> %d, restarting stability "
                        "counter)",
                        execution_id, count, last_count, count,
                    )
                stable_polls = 0
                pending = [
                    s for s in steps
                    if s.get("status") != self.STEP_STATUS_SUCCESS
                ]
                if pending:
                    current = pending[0]
                    desc = (
                        f"{current.get('description', '')} "
                        f"(status={current.get('status')})"
                    )
                    if desc != last_desc:
                        logger.info(
                            "  NetApp Shift execution step: %s "
                            "(%d/%d steps at status=4)",
                            desc, count - len(pending), count,
                        )
                        last_desc = desc

            last_count = count

            if time.monotonic() >= deadline:
                raise NetAppShiftError(
                    f"NetApp Shift execution {execution_id} did not finish "
                    f"within {timeout}s (last step: {last_desc or 'unknown'})"
                )
            time.sleep(poll_interval)
