"""The protected resource: one MCP server, one mode, one downstream resource.

Everything identity-related happens here, and it is the same in both modes:

1. `AzureJWTVerifier` validates the incoming bearer token against this app's own
   audience, the tenant issuer, and the `Mcp.Tools.ReadWrite` scope. A token
   minted for a different resource is rejected before any tool body runs.
2. `OboExchange` trades that token, plus this pod's projected Workload Identity
   token, for a token to a downstream resource that still carries the user.

`MCP_MODE` then decides which downstream resource that is:

    orders      -> src/orders.py      -> Microsoft Graph
    databricks  -> src/databricks.py  -> Azure Databricks + Unity Catalog

The two are alternatives, not layers. Each mode is a complete example on its
own, so a reader follows exactly one path. Adding a third resource means adding
a module with a `register` function and one line below -- see docs/authorization.md
for why the exchange is the only part that has to be shared.

Neither mode has an application-permission path to its data. Without a caller
there is nothing to exchange, and both `Graph /me` and Databricks
`current_user()` are meaningless outside user context.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from pydantic import AnyHttpUrl
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src import databricks as databricks_tools
from src import orders as order_tools
from src.obo import OboExchange

ORDERS_MODE = "orders"
DATABRICKS_MODE = "databricks"
MODES = (ORDERS_MODE, DATABRICKS_MODE)

# Pinned rather than left to the fastmcp default, because infra/main.bicep
# publishes `MCP_ENDPOINT` as this exact path.
MCP_PATH = "/mcp"

# The RFC 9728 discovery documents are read before a client has a token, from
# wherever that client happens to run, so they are the one thing the Origin
# check below lets through.
DISCOVERY_PREFIX = "/.well-known/"

_MODE_NAMES = {
    ORDERS_MODE: ("Order Handling", "FastMCP Order Handling"),
    DATABRICKS_MODE: ("Databricks Orders", "FastMCP Databricks Orders"),
}


class SameOriginOnly:
    """Refuses a request whose `Origin` is not one this deployment serves.

    The MCP transport spec puts a MUST on validating `Origin`, and the SDK does
    ship `TransportSecurityMiddleware` for it, but fastmcp 3.2.4 constructs it
    with no settings, so DNS-rebinding protection defaults off and the check
    returns before it reads the header.

    Nothing is riding on this today: /mcp reads no cookies, it authorizes a
    bearer token that a page on another origin cannot obtain, and no CORS
    headers are served that would let one read the response. It is here because
    the chat app sets a `SameSite=none` cookie on this same ingress host, which
    is one change away from making ambient credentials reachable, and because a
    header comparison is a cheaper thing to own than that argument.
    """

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and not path.startswith(DISCOVERY_PREFIX):
            # Everything but discovery is checked, rather than naming /mcp,
            # so an ingress that rewrites the path cannot quietly turn this
            # off. Only a browser attaches Origin and only a browser is subject
            # to the attack, so its absence is not a failure to report: the
            # chat app calls this endpoint from its own process and sends none.
            origin = Headers(scope=scope).get("origin")
            if origin and origin not in self.allowed:
                await PlainTextResponse("Origin not allowed", status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def allowed_origins() -> frozenset[str]:
    """This server's own origin, plus any an operator adds for a browser client."""
    extra = os.getenv("MCP_ALLOWED_ORIGINS", "").split(",")
    origins = (_origin(url) for url in (os.getenv("MCP_SERVER_URL", ""), *extra))
    return frozenset(origin for origin in origins if origin)


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def current_mode() -> str:
    mode = os.getenv("MCP_MODE", ORDERS_MODE).strip().lower() or ORDERS_MODE
    if mode not in MODES:
        raise RuntimeError(f"MCP_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    return mode


def create_server() -> FastMCP:
    mode = current_mode()
    tenant_id = _required_env("AZURE_TENANT_ID")
    client_id = _required_env("ENTRA_SERVER_CLIENT_ID")
    server_url = _required_env("MCP_SERVER_URL").rstrip("/")
    token_file = Path(
        os.getenv(
            "AZURE_FEDERATED_TOKEN_FILE",
            "/var/run/secrets/azure/tokens/azure-identity-token",
        )
    )
    server_name, resource_name = _MODE_NAMES[mode]

    verifier = AzureJWTVerifier(
        client_id=client_id,
        tenant_id=tenant_id,
        required_scopes=["Mcp.Tools.ReadWrite"],
    )
    auth = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[
            AnyHttpUrl(f"https://login.microsoftonline.com/{tenant_id}/v2.0")
        ],
        base_url=server_url,
        resource_name=resource_name,
    )
    mcp = FastMCP(
        server_name,
        auth=auth,
        mask_error_details=True,
        strict_input_validation=True,
    )

    def workload_assertion() -> str:
        # AKS Workload Identity projects a short-lived signed token for this
        # pod's service account. It takes the place of a client secret, which
        # is why no credential is stored anywhere in this deployment.
        assertion = token_file.read_text(encoding="utf-8").strip()
        if not assertion:
            raise RuntimeError("The projected workload identity token is empty")
        return assertion

    exchange = OboExchange(
        tenant_id=tenant_id,
        client_id=client_id,
        assertion=workload_assertion,
    )

    if mode == ORDERS_MODE:
        order_tools.register(mcp, exchange)
    else:
        settings = databricks_tools.DatabricksSettings.from_env()
        if settings is None:
            raise RuntimeError(
                "MCP_MODE=databricks requires DATABRICKS_HOST. Run "
                "`azd provision` with deploymentMode=databricks, which deploys "
                "the workspace and produces it."
            )
        databricks_tools.register(mcp, exchange, settings)

    return mcp


if __name__ == "__main__":
    create_server().run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path=MCP_PATH,
        middleware=[Middleware(SameOriginOnly, allowed=allowed_origins())],
        show_banner=False,
    )
