"""NetApp Shift REST API client.

Talks to the multi-port NetApp Shift API surface:

  - :3698 — tenant sessions (login/logout)
  - :3700 — setup APIs (sites, VMs, resource groups, blueprints)
  - :3704 — recovery APIs (migrate execution + status polling)

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
        client.trigger_migration(bp_id)
        client.wait_for_migration(bp_id)
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

    def get_vm_id_by_name(self, site_id: str, virt_env_id: str, vm_name: str) -> str:
        payload = self._request(
            "GET",
            SETUP_PORT,
            "/api/setup/vm/unprotected",
            params={"siteId": site_id, "virtEnvId": virt_env_id},
        )
        for vm in payload.get("list", []) or []:
            if vm.get("name") == vm_name:
                vm_id = vm.get("_id")
                if not vm_id:
                    raise NetAppShiftError(
                        f"NetApp Shift VM '{vm_name}' is missing _id"
                    )
                return vm_id
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
        qtree_name: str,
        boot_order: int = 3,
    ) -> str:
        body = {
            "name": name,
            "sourceSite": {"_id": source_site_id},
            "sourceVirtEnv": {"_id": source_virt_env_id},
            "vms": [{"_id": vm_id}],
            "bootOrder": {
                "vms": [{"vm": {"_id": vm_id}, "order": boot_order}],
            },
            "bootDelay": [
                {"vm": {"_id": vm_id}, "delaySecs": 30},
            ],
            "scripts": [],
            "replicationPlan": {
                "targetSite": {"_id": dest_site_id},
                "targetVirtEnv": {"_id": dest_virt_env_id},
                "datastoreQtreeMapping": [
                    {
                        "vm": {"_id": vm_id},
                        "datastoreName": datastore_name,
                        "qtreeName": qtree_name,
                        "volumeName": qtree_name,
                    }
                ],
                "snapshotType": "clone_based_migration",
                "frequencyMins": "30",
                "retryCount": 3,
                "numSnapshotsToRetain": 2,
            },
            "migrationMode": "clone_based_migration",
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
        vm_id: str,
        vm_name: str,
    ) -> str:
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
                    {"protectionGroup": {"_id": resource_group_id}, "order": 0},
                ],
                "vms": [
                    {"vm": {"_id": vm_id}, "order": 0},
                ],
            },
            "vmSettings": [
                {
                    "vm": {"_id": vm_id},
                    "name": vm_name,
                    "order": 0,
                    "powerOnFlag": True,
                }
            ],
            "mappings": [],
            "ipConfig": {"type": "retain", "targetNetworks": []},
            "serviceAccounts": [],
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

    def get_resource_group_id_by_name(self, name: str) -> str | None:
        payload = self._request("GET", SETUP_PORT, "/api/setup/protectionGroup")
        for rg in payload.get("list", []) or []:
            if rg.get("name") == name:
                return rg.get("_id")
        return None

    def get_blueprint_id_by_name(self, name: str) -> str | None:
        payload = self._request("GET", SETUP_PORT, "/api/setup/drplan")
        for bp in payload.get("list", []) or []:
            if bp.get("name") == name:
                return bp.get("_id")
        return None

    # ------------------------------------------------------------------
    # Migration execution + status
    # ------------------------------------------------------------------

    def trigger_migration(self, blueprint_id: str) -> str:
        body = {
            "serviceAccounts": {
                "common": {"loginId": None, "password": None},
                "vms": [],
            }
        }
        payload = self._request(
            "POST",
            RECOVERY_PORT,
            f"/api/recovery/drPlan/{blueprint_id}/migrate/execution",
            json=body,
        )
        execution_id = payload.get("_id")
        if not execution_id:
            raise NetAppShiftError(
                f"NetApp Shift trigger migration response missing _id: {payload}"
            )
        return execution_id

    def get_migration_status(self, blueprint_id: str) -> str:
        payload = self._request(
            "GET", RECOVERY_PORT, "/api/recovery/drplan/status",
        )
        items = payload.get("list", []) if isinstance(payload, dict) else payload
        for entry in items or []:
            dr_plan = entry.get("drPlan") or {}
            if dr_plan.get("_id") == blueprint_id:
                return dr_plan.get("recoveryStatus") or ""
        return ""

    def wait_for_migration(
        self,
        blueprint_id: str,
        *,
        poll_interval: int = 30,
        timeout: int = 14400,
    ) -> str:
        deadline = time.monotonic() + timeout
        last_status = ""
        while True:
            status = self.get_migration_status(blueprint_id)
            if status and status != last_status:
                logger.info("  NetApp Shift migration status: %s", status)
                last_status = status
            if status and ("complete" in status or "error" in status):
                return status
            if time.monotonic() >= deadline:
                raise NetAppShiftError(
                    f"NetApp Shift migration {blueprint_id} did not finish "
                    f"within {timeout}s (last status: {status or 'unknown'})"
                )
            time.sleep(poll_interval)
