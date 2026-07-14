"""
Two-probe diagnostic for Cityworks 23 cookie auth.

Probe A: Send the entire Cookie header exactly as Chrome sent it — no jar,
         no encoding changes, no policy checks. If this works, the issue was
         in how we were building the cookie jar (likely URL-decoding values
         that must stay encoded).

Probe B: Send only the CwAuthenticationTicket cookies via raw header,
         to find the minimum needed.

Instructions:
    1. Open Chrome DevTools -> Network
    2. Click any request to /Cityworks23/services/...
    3. Right-click the "Cookie" header value -> Copy value
    4. Paste it as CW_COOKIE_HEADER in your .env (wrap in double quotes)
    5. Set CW_PROBE_SID to a work order SID you can read

.env example:
    CW_COOKIE_HEADER="nmstat=abc; CwAuthenticationTicket=gYHBk...; CwAuthenticationTicket_Athena=.enc%3A..."
    CW_PROBE_SID=233148
"""

import json
import os
import requests

BASE   = "https://cmms.cityofmadison.com/Cityworks23/services"
DOMAIN = "cmms.cityofmadison.com"

FULL_COOKIE_HEADER = os.getenv("CW_COOKIE_HEADER", "")
READ_SID = int(os.getenv("CW_PROBE_SID", "233148"))


def probe(label: str, cookie_header: str) -> bool:
    s = requests.Session()
    # Send Cookie as a raw header — bypasses RequestsCookieJar entirely
    s.headers.update({
        "Accept": "application/json",
        "Cookie": cookie_header,
    })

    resp = s.get(
        f"{BASE}/Ams/WorkOrder/BySid",
        params={"data": json.dumps({"WorkOrderSid": READ_SID})},
        timeout=30,
    )

    print(f"\n--- {label} ---")
    print(f"HTTP {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('Content-Type', 'unknown')}")

    ctype = resp.headers.get("Content-Type", "")
    if "json" not in ctype:
        print("Response is HTML (not JSON) — being redirected to login page")
        print(f"First 300 chars: {resp.text[:300]}")
        return False

    body = resp.json()
    print(f"Status:        {body.get('Status')}")
    print(f"ErrorMessages: {body.get('ErrorMessages')}")
    value = body.get("Value")
    print(f"Value is None: {value is None}")
    if value is not None:
        desc = value.get("Description") if isinstance(value, dict) else str(value)[:100]
        print(f"Description:   {desc}")

    ok = body.get("Status") == 0 and value is not None
    print(f"Result:        {'PASS' if ok else 'FAIL'}")
    return ok


def extract_tickets(full_header: str) -> str:
    """Pull only the two CwAuthentication cookies from the full header."""
    parts = [p.strip() for p in full_header.split(";")]
    tickets = [p for p in parts if p.startswith("CwAuthentication")]
    return "; ".join(tickets)


if __name__ == "__main__":
    if not FULL_COOKIE_HEADER:
        print("Set CW_COOKIE_HEADER in .env first.")
        print("Copy the entire Cookie header value from Chrome DevTools.")
        raise SystemExit(1)

    # Probe A: the complete cookie header, exactly as Chrome sent it
    probe("A: Full cookie header (exactly as Chrome sent)", FULL_COOKIE_HEADER)

    # Probe B: only the two auth tickets
    tickets_only = extract_tickets(FULL_COOKIE_HEADER)
    print(f"\nTicket cookies extracted: {tickets_only[:80]}...")
    probe("B: CwAuthenticationTicket cookies only (raw)", tickets_only)
