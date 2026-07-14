"""Authenticate to Cityworks 23 by reusing the browser's session cookie.

Background
----------
This install federates login through ArcGIS Portal ("Athena") with Entra ID
MFA.  The Cityworks REST API cannot mint a token for an interactive account,
but it accepts the session cookies the web UI already holds.

Two ways to get those cookies into a requests.Session:

    cookies_from_env()     -- paste the values into .env; always works
    cookies_from_browser() -- automatic read from Firefox; may work

Both produce a cookie jar you can hand to CityworksSession(cookies=...).

Known cookie names for this install
------------------------------------
Required for reads (minimum, determine exact set with find_minimal_auth.py):
    CwAuthenticationTicket          -- ASP.NET forms-auth ticket
    CwAuthenticationTicket_Athena   -- Portal/ArcGIS-integrated auth ticket

Possibly required for reads:
    .ASPROLES           -- encrypted role cache
    ASP.NET_SessionId   -- server-side session handle

Required for writes (CSRF protection):
    cw-reqtkn           -- anti-CSRF token; rotates each session

Noise (analytics / UI state -- never needed):
    nmstat, _ga, _ga_*, __utma, __utmc, __utmz
    cw-visible-info, SelectableLayerData, mode

Expiry
------
All auth cookies expire when the Entra conditional-access policy decides to
re-challenge MFA -- typically daily, sometimes longer.  The safe pattern:
    1. Log into Cityworks in the browser (complete MFA if prompted).
    2. Grab fresh values and update your .env.
    3. Run preflight() before any bulk write job.
"""

from typing import Optional
from http.cookies import SimpleCookie
import os

import requests

DOMAIN = "cmms.cityofmadison.com"

# The cookies we care about, in priority order.
# Determined by find_minimal_auth.py against this specific install.
AUTH_COOKIE_NAMES = [
    "CwAuthenticationTicket",
    "CwAuthenticationTicket_Athena",
    ".ASPROLES",
    "ASP.NET_SessionId",
    "cw-reqtkn",           # needed for write operations (CSRF)
]

# Env-var names that map to each cookie
ENV_VAR_MAP = {
    "CwAuthenticationTicket":         "CW_TICKET",
    "CwAuthenticationTicket_Athena":  "CW_TICKET_ATHENA",
    ".ASPROLES":                      "CW_ASPROLES",
    "ASP.NET_SessionId":              "CW_SESSIONID",
    "cw-reqtkn":                      "CW_REQTKN",
}


class CookieAuthError(Exception):
    """Raised when a usable Cityworks cookie can't be obtained."""


# ---------------------------------------------------------------------------
# Manual path -- paste the Cookie header from DevTools
# ---------------------------------------------------------------------------

def cookies_from_string(cookie_header: str) -> requests.cookies.RequestsCookieJar:
    """Parse a raw ``Cookie:`` header string into a jar.

    Grab from DevTools:
        F12 -> Network -> any /Cityworks23/services/... request
        -> Headers -> Request Headers -> right-click Cookie value -> Copy value

    Args:
        cookie_header: e.g. ``"CwAuthenticationTicket=abc; .ASPROLES=def"``

    Returns:
        Cookie jar containing only the auth-relevant cookies.
    """
    if not cookie_header or "=" not in cookie_header:
        raise CookieAuthError("Cookie header looks empty or malformed.")

    parsed = SimpleCookie()
    parsed.load(cookie_header)

    jar = requests.cookies.RequestsCookieJar()
    for name, morsel in parsed.items():
        if name in AUTH_COOKIE_NAMES:
            jar.set(name, morsel.value, domain=DOMAIN, path="/")

    if not len(jar):
        raise CookieAuthError(
            "No recognised auth cookies found in that header. "
            f"Expected one or more of: {AUTH_COOKIE_NAMES}"
        )
    return jar


def cookies_from_env() -> requests.cookies.RequestsCookieJar:
    """Load cookie values from environment variables (or a .env file).

    Put these in your .env (already gitignored):

        CW_TICKET=<CwAuthenticationTicket value>
        CW_TICKET_ATHENA=<CwAuthenticationTicket_Athena value>
        CW_ASPROLES=<.ASPROLES value>
        CW_SESSIONID=<ASP.NET_SessionId value>
        CW_REQTKN=<cw-reqtkn value>

    All values should be URL-decoded (no %3A, %3D etc).

    Refresh after each login / MFA challenge.
    """
    from urllib.parse import unquote

    jar = requests.cookies.RequestsCookieJar()
    found = []

    for cookie_name, env_var in ENV_VAR_MAP.items():
        raw = os.getenv(env_var, "")
        if raw:
            jar.set(cookie_name, unquote(raw), domain=DOMAIN, path="/")
            found.append(cookie_name)

    if not found:
        raise CookieAuthError(
            "No CW_* environment variables are set. "
            "Copy your cookie values from DevTools and add them to .env. "
            "See ENV_VAR_MAP in this module for the variable names."
        )

    print(f"[cityworks] cookies loaded from env: {found}")
    return jar


# ---------------------------------------------------------------------------
# Automatic path -- read the browser's cookie store
# ---------------------------------------------------------------------------

def cookies_from_browser(browser: str = "firefox") -> requests.cookies.RequestsCookieJar:
    """Read Cityworks auth cookies from a local browser's cookie store.

    Args:
        browser: 'firefox', 'edge', or 'chrome'.
            Firefox is reliable on Windows.
            Chrome/Edge use App-Bound Encryption which usually blocks this.

    Raises:
        CookieAuthError: store can't be read, or holds none of the known cookies.
    """
    try:
        import browser_cookie3
    except ImportError:
        raise CookieAuthError("Run: pip install browser-cookie3")

    try:
        raw = getattr(browser_cookie3, browser)(domain_name=DOMAIN)
    except Exception as e:
        raise CookieAuthError(
            f"Couldn't read {browser} cookie store: {e}\n"
            "On Windows, Chrome/Edge App-Bound Encryption usually blocks this.\n"
            "Try Firefox, or use cookies_from_env() instead."
        )

    jar = requests.cookies.RequestsCookieJar()
    found = []
    for c in raw:
        if c.name in AUTH_COOKIE_NAMES:
            jar.set(c.name, c.value, domain=c.domain, path=c.path)
            found.append(c.name)

    if not found:
        raise CookieAuthError(
            f"No Cityworks auth cookies found in {browser}. "
            "Are you logged into Cityworks in that browser right now?"
        )

    print(f"[cityworks] cookies loaded from {browser}: {found}")
    return jar


# ---------------------------------------------------------------------------
# Convenience: try browser first, fall back to env
# ---------------------------------------------------------------------------

def get_cookies(browser: Optional[str] = "firefox") -> requests.cookies.RequestsCookieJar:
    """Best-effort cookie load.

    Tries the browser store first (if a browser is specified), then falls
    back to environment variables.

    Args:
        browser: Browser to try first ('firefox', 'edge', 'chrome'), or None
                 to skip straight to env vars.
    """
    if browser:
        try:
            return cookies_from_browser(browser)
        except CookieAuthError as e:
            print(f"[cityworks] browser load failed: {e}")
            print("[cityworks] falling back to env vars")
    return cookies_from_env()
