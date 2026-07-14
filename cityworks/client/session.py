"""Cityworks session management — cookie auth for CW23.

After the 23.15 upgrade the live application moved to /cityworks/Services/.
That endpoint accepts the browser session cookie (CwAuthenticationTicket +
CwAuthenticationTicket_Athena) rather than requiring an Authorization token.

Usage with cookies (daily workflow):
    from cityworks.client.browser_auth import get_cookies
    cookies = get_cookies("firefox")          # automatic from Firefox
    # or: cookies = cookies_from_string("...") # paste from Chrome DevTools

    config = CityworksConfig(environment="prod")
    with CityworksSession(config, cookies=cookies) as session:
        wo_api = WorkOrderAPI(session)
        session.preflight()   # verify session is live before writing
        ...

Usage with token (if an API-Only service account is provisioned later):
    with CityworksSession(config, token="eyJ...") as session:
        ...
"""

from typing import Optional, Dict, Any
import os
import time
import getpass
import json
import requests

from .response import CityworksResponse
from .exceptions import CityworksAuthenticationError, CityworksAPIError
from ..config import CityworksConfig


class CityworksSession:
    """Authenticated session for the Cityworks REST API."""

    AUTH_ENDPOINT = "/General/Authentication"
    TOKEN_ENV_VAR = "CITYWORKS_TOKEN"

    # A known-good read-only endpoint used for preflight checks.
    # Override if this SID ever gets deleted.
    PROBE_WO_SID = 233148

    def __init__(
        self,
        config: CityworksConfig,
        token: Optional[str] = None,
        cookies: Optional[requests.cookies.RequestsCookieJar] = None,
    ):
        self.config = config
        self._token: Optional[str] = None
        self._auth_mode = "unauthenticated"
        self._last_request_time = 0.0

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        if cookies is not None:
            self._session.cookies.update(cookies)
            self._auth_mode = "cookie"
        elif token or os.getenv(self.TOKEN_ENV_VAR):
            self.token = token or os.getenv(self.TOKEN_ENV_VAR)  # setter installs header

    # ------------------------------------------------------------------
    # Token property (only used for token auth mode)
    # ------------------------------------------------------------------

    @property
    def token(self) -> Optional[str]:
        return self._token

    @token.setter
    def token(self, value: Optional[str]) -> None:
        """Install the token in the Authorization header (CW23 format)."""
        self._token = value
        if value:
            self._session.headers["Authorization"] = f"cityworks {value}"
            self._auth_mode = "token"
        else:
            self._session.headers.pop("Authorization", None)

    @property
    def is_authenticated(self) -> bool:
        return self._auth_mode in ("token", "cookie")

    # ------------------------------------------------------------------
    # Token authentication (for future API-Only service account use)
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> str:
        """Authenticate via username/password and store token.

        Requires an API-Only account (not subject to MFA federation).
        For cookie-based auth, use CityworksSession(config, cookies=...) instead.
        """
        url = self.config.base_url + f"{self.AUTH_ENDPOINT}/Authenticate"
        payload = {"data": json.dumps({"LoginName": username, "Password": password})}

        try:
            response = self._session.post(url, data=payload, timeout=self.config.timeout)
        except requests.RequestException as e:
            raise CityworksAuthenticationError(f"Authentication request failed: {e}")

        if response.status_code == 401:
            raise CityworksAuthenticationError(
                "401 Unauthorized. In CW23 an integration account must be "
                "flagged 'API Only' and excluded from MFA."
            )

        cw = CityworksResponse(response)
        if not cw.is_success or not cw.value:
            msg = "; ".join(cw.error_messages) if cw.error_messages else "Authentication failed"
            raise CityworksAuthenticationError(msg)

        token = cw.value.get("Token")
        if not token:
            raise CityworksAuthenticationError(f"No Token in response: {cw.value}")

        self.token = token
        return token

    def prompt_credentials(self, username: Optional[str] = None) -> str:
        if username is None:
            username = input(f"Cityworks login [{getpass.getuser()}]: ").strip() or getpass.getuser()
        password = getpass.getpass("Password: ")
        return self.authenticate(username, password)

    # ------------------------------------------------------------------
    # Preflight check
    # ------------------------------------------------------------------

    def preflight(self, wo_sid: Optional[int] = None) -> None:
        """Verify the session is live before doing any writes.

        Call this at the start of any batch script. A dead cookie fails here
        with a clear message rather than mid-write with a confusing error.

        Args:
            wo_sid: WorkOrderSid to probe (defaults to PROBE_WO_SID).

        Raises:
            CityworksAuthenticationError: with instructions to refresh cookie.
        """
        sid = wo_sid or self.PROBE_WO_SID
        try:
            self.get("Ams", "WorkOrder", "BySid", {"WorkOrderSid": sid})
        except CityworksAuthenticationError:
            mode_hint = (
                "Refresh your browser cookies and update FULL_COOKIE_HEADER."
                if self._auth_mode == "cookie"
                else "Token may be expired."
            )
            raise CityworksAuthenticationError(
                f"Cityworks session is not live. {mode_hint} "
                "Nothing was written."
            )

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def get(
        self,
        service_category: str,
        service: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> CityworksResponse:
        """GET request — used for read operations."""
        self._ensure_authenticated()
        self._apply_rate_limit()
        url = self.config.get_endpoint_url(service_category, service, endpoint)
        resp = self._session.get(
            url,
            params={"data": json.dumps(params or {})},
            timeout=self.config.timeout,
        )
        self._last_request_time = time.time()
        self._check_response(resp)
        return CityworksResponse(resp)

    def post(
        self,
        service_category: str,
        service: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> CityworksResponse:
        """POST request — used for write operations."""
        self._ensure_authenticated()
        self._apply_rate_limit()
        url = self.config.get_endpoint_url(service_category, service, endpoint)
        resp = self._session.post(
            url,
            data={"data": json.dumps(params or {})},
            timeout=self.config.timeout,
        )
        self._last_request_time = time.time()
        self._check_response(resp)
        return CityworksResponse(resp)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Kept for backward compat. Token is NOT included here in CW23."""
        return {"data": json.dumps(params)}

    def _ensure_authenticated(self) -> None:
        if not self.is_authenticated:
            raise CityworksAuthenticationError(
                "No credentials. Pass cookies= or token= to CityworksSession(), "
                "or call authenticate()."
            )

    @staticmethod
    def _check_response(response: requests.Response) -> None:
        """Raise clearly on HTTP-level auth failures."""
        if response.status_code in (401, 403):
            raise CityworksAuthenticationError(
                f"HTTP {response.status_code} — session may have expired."
            )
        # An expired cookie causes a redirect to the ArcGIS/Entra login page,
        # which returns HTTP 200 with HTML. Catch it before it becomes a
        # confusing JSON parse error downstream.
        if "text/html" in response.headers.get("Content-Type", ""):
            raise CityworksAuthenticationError(
                "Got an HTML login page instead of JSON. "
                "Your Cityworks session cookie has expired — log in again "
                "in the browser and refresh your cookies."
            )

    def _apply_rate_limit(self) -> None:
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.config.rate_limit_delay:
                time.sleep(self.config.rate_limit_delay - elapsed)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._session.close()