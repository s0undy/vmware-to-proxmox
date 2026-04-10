"""NetApp Shift REST API client.

Foundation only. The client handles connection, session-based authentication,
request dispatch, and error mapping. Disk-operation methods are stubbed — they
will be wired up in the follow-up pass once the exact NetApp Shift endpoints,
payloads, and job model are confirmed.
"""

import logging
import urllib3

import requests

from .config import NetAppShiftConfig
from .exceptions import NetAppShiftConnectionError, NetAppShiftError

logger = logging.getLogger(__name__)

# TODO: confirm actual endpoint paths against NetApp Shift API docs.
LOGIN_PATH = "/api/v1/login"
LOGOUT_PATH = "/api/v1/logout"
HEALTH_PATH = "/api/v1/version"

DEFAULT_TIMEOUT = 30


class NetAppShiftClient:
    """Thin REST client for NetApp Shift.

    Usage:
        client = NetAppShiftClient(config)
        client.connect()           # login, stores session token
        client.health()            # smoke test
        client.close()             # optional logout
    """

    def __init__(self, config: NetAppShiftConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}"
        self.session = requests.Session()
        self.session.verify = config.verify_ssl
        self._token: str | None = None

        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def connect(self) -> None:
        """Authenticate and store a session token."""
        logger.info("Connecting to NetApp Shift at %s ...", self.base_url)
        try:
            response = self.session.post(
                f"{self.base_url}{LOGIN_PATH}",
                json={"username": self.config.user, "password": self.config.password},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise NetAppShiftConnectionError(
                f"Failed to reach NetApp Shift at {self.base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise NetAppShiftConnectionError(
                f"NetApp Shift login failed ({response.status_code}): {response.text}"
            )

        # TODO: confirm the response shape — some APIs return the token in a
        # JSON body field, others via a Set-Cookie header. Prefer the body
        # field when present, otherwise trust the session's cookie jar.
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        self._token = payload.get("token") or payload.get("access_token")
        if self._token:
            self.session.headers["Authorization"] = f"Bearer {self._token}"

        logger.info("  NetApp Shift: authenticated as %s", self.config.user)

    def close(self) -> None:
        """Best-effort logout. Safe to call multiple times."""
        if self._token is None:
            return
        try:
            self.session.post(f"{self.base_url}{LOGOUT_PATH}", timeout=DEFAULT_TIMEOUT)
        except requests.RequestException:
            pass
        self._token = None
        self.session.headers.pop("Authorization", None)

    def health(self) -> dict:
        """Smoke-test GET against the API."""
        return self._request("GET", HEALTH_PATH)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Dispatch a request, raising NetAppShiftError on failure."""
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise NetAppShiftError(
                f"NetApp Shift request failed ({method} {path}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise NetAppShiftError(
                f"NetApp Shift {method} {path} returned {response.status_code}: "
                f"{response.text}"
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise NetAppShiftError(
                f"NetApp Shift {method} {path} returned non-JSON body: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Disk-operation stubs — wired up in the follow-up pass.
    # ------------------------------------------------------------------

    def convert_vmdk(self, *args, **kwargs) -> dict:
        raise NotImplementedError("NetAppShiftClient.convert_vmdk not yet implemented")

    def get_job_status(self, job_id: str) -> dict:
        raise NotImplementedError("NetAppShiftClient.get_job_status not yet implemented")

    def wait_for_job(self, job_id: str, timeout: int = 3600) -> dict:
        raise NotImplementedError("NetAppShiftClient.wait_for_job not yet implemented")
