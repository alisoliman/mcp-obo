"""Values and small helpers shared by more than one test module."""

from __future__ import annotations

import json
import re
import time
from base64 import urlsafe_b64encode
from http.cookies import Morsel, SimpleCookie
from pathlib import Path

import httpx

from src.chat_app.sessions import COOKIE_NAME

ROOT = Path(__file__).resolve().parents[1]

MCP_ENVIRONMENT = {
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "ENTRA_SERVER_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "MCP_SERVER_URL": "https://orders.example.test",
    "MCP_MODE": "orders",
}
DATABRICKS_ENVIRONMENT = {
    "DATABRICKS_HOST": "https://adb-1234567890.11.azuredatabricks.net",
    "DATABRICKS_WAREHOUSE_ID": "0123456789abcdef",
    "DATABRICKS_CATALOG": "mcp_obo_demo",
    "DATABRICKS_SCHEMA": "sales",
}

# An ARM `createObject` renders its arguments as a flat, comma-separated list, so
# a resourceAccess entry inside a compiled expression string reads as
# `'type', 'Scope'` rather than as JSON.
ARM_ACCESS_TYPE = re.compile(r"'type',\s*'([A-Za-z]+)'")
# `az bicep build` reports a template it cannot compile with a BCP diagnostic
# code, so a failure carrying none of those and phrased like one of these is the
# toolchain being absent rather than the template being broken.
BICEP_ABSENT = ("not installed", "could not", "unable to", "no such file")


def csp_directives(response: httpx.Response) -> dict[str, list[str]]:
    """The Content-Security-Policy header, split into directive -> values."""
    policy = response.headers["content-security-policy"]
    directives = (directive.split() for directive in policy.split(";"))
    return {parts[0]: parts[1:] for parts in directives if parts}


def session_cookie(response: httpx.Response) -> Morsel:
    """The session cookie this response set, attributes included.

    Read off `Set-Cookie` rather than out of the TestClient's jar, because the
    jar keeps the value and discards everything the `__Host-` prefix depends on.
    """
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    if COOKIE_NAME not in jar:
        raise AssertionError(f"no {COOKIE_NAME} cookie on the response")
    return jar[COOKIE_NAME]


def user_assertion(subject: str = "ada", expires_in: float = 3_600.0) -> str:
    """A JWT-shaped caller token, built with base64 rather than a JWT library.

    Nothing in `src/mcp_server/obo.py` verifies this: `AzureJWTVerifier` already did, and
    the only claim read afterwards is `exp`, which decides how long a cache entry
    may live. Signing it here would test a library rather than the cache.
    """
    claims = {"sub": subject, "exp": time.time() + expires_in}
    payload = urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
    return f"header.{payload.decode('ascii')}.signature"
