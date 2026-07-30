"""FastMCP order server: the protected resource in the delegated OBO flow.

Two identity steps happen before any tool body runs:

1. `AzureJWTVerifier` validates the incoming bearer token against this app's
   own audience, the tenant issuer, and the `Mcp.Tools.ReadWrite` scope. A
   token minted for a different resource is rejected.
2. `current_user()` performs the on-behalf-of exchange with Microsoft Entra ID,
   using the projected AKS Workload Identity token as this server's credential,
   and calls Graph `/me`. The Graph object ID it returns is the only key the
   order store is partitioned by.

There is no application-permission path to order data: without a caller there
is nothing to exchange, and Graph `/me` has no meaning outside user context.

The same caller token also reaches a second downstream resource. When
`DATABRICKS_HOST` is set, `src/databricks.py` registers tools that exchange it
for an Azure Databricks token instead of a Graph one and let Unity Catalog do
the row-level authorization this module does by hand. One delegated token, two
resources, one exchange -- see `src/obo.py`.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from enum import StrEnum
from itertools import count
from pathlib import Path
from typing import Annotated

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, RemoteAuthProvider
from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from fastmcp.server.dependencies import CurrentAccessToken
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints

from src import databricks as databricks_tools
from src.obo import GRAPH_SCOPE, OboExchange

Sku = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]
OrderId = Annotated[str, StringConstraints(pattern=r"^ORD-\d{6}$")]


class OrderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class OrderItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    sku: Sku
    quantity: int = Field(gt=0, le=1_000)
    unit_price_cents: int = Field(ge=0, le=100_000_000)


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: OrderId
    status: OrderStatus
    items: tuple[OrderItem, ...]
    total_cents: int
    created_by: str


class GraphUser(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    display_name: str = Field(alias="displayName")
    user_principal_name: str | None = Field(default=None, alias="userPrincipalName")


class IdentityProof(BaseModel):
    model_config = ConfigDict(frozen=True)

    display_name: str
    identity_fingerprint: str
    evidence: str = "Microsoft Graph /me via OBO"


_orders: dict[str, dict[str, Order]] = defaultdict(dict)
_order_numbers = count(1)
_allowed_transitions = {
    OrderStatus.PENDING: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


def create_order_for(
    owner_id: str,
    owner_name: str,
    items: list[OrderItem],
) -> Order:
    if not items:
        raise ValueError("An order needs at least one item")

    order = Order(
        order_id=f"ORD-{next(_order_numbers):06d}",
        status=OrderStatus.PENDING,
        items=tuple(items),
        total_cents=sum(item.quantity * item.unit_price_cents for item in items),
        created_by=owner_name,
    )
    _orders[owner_id][order.order_id] = order
    return order


def list_orders_for(owner_id: str) -> list[Order]:
    return sorted(_orders[owner_id].values(), key=lambda order: order.order_id)


def get_order_for(owner_id: str, order_id: str) -> Order:
    try:
        return _orders[owner_id][order_id]
    except KeyError:
        raise ValueError("Order not found") from None


def update_order_status_for(
    owner_id: str,
    order_id: str,
    status: OrderStatus,
) -> Order:
    order = get_order_for(owner_id, order_id)
    if status == order.status:
        return order
    if status not in _allowed_transitions[order.status]:
        raise ValueError(f"Cannot move an order from {order.status} to {status}")

    updated = order.model_copy(update={"status": status})
    _orders[owner_id][order_id] = updated
    return updated


def identity_proof_for(user: GraphUser) -> IdentityProof:
    fingerprint = hashlib.sha256(user.id.encode("utf-8")).hexdigest()[:8].upper()
    return IdentityProof(
        display_name=user.display_name,
        identity_fingerprint=fingerprint,
    )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def create_server() -> FastMCP:
    tenant_id = _required_env("AZURE_TENANT_ID")
    client_id = _required_env("ENTRA_SERVER_CLIENT_ID")
    server_url = _required_env("MCP_SERVER_URL").rstrip("/")
    token_file = Path(
        os.getenv(
            "AZURE_FEDERATED_TOKEN_FILE",
            "/var/run/secrets/azure/tokens/azure-identity-token",
        )
    )

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
        resource_name="FastMCP Order Handling",
    )
    mcp = FastMCP(
        "Order Handling",
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

    async def current_user(access_token: AccessToken) -> GraphUser:
        # FastMCP ships an EntraOBOToken dependency, but it only resolves when
        # the server's auth provider is, or wraps, an AzureProvider — FastMCP's
        # OAuth proxy, which mounts /authorize, /token, dynamic client
        # registration and a consent screen. This server is purely a resource
        # server sitting behind a pre-registered confidential client, so it
        # needs none of that, and a secretless AzureProvider still demands a
        # jwt_signing_key. Running the exchange here keeps the deployment at
        # one route and no secrets.
        #
        # Every tool goes through here, so no tool can read data for anyone but
        # the caller.
        graph_token = await exchange.token(access_token.token, GRAPH_SCOPE)

        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                params={"$select": "id,displayName,userPrincipalName"},
                headers={"Authorization": f"Bearer {graph_token}"},
            )
            response.raise_for_status()
            return GraphUser.model_validate(response.json())

    @mcp.tool()
    async def who_am_i(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> IdentityProof:
        """Show the safe Graph identity proof resolved through OBO."""
        user = await current_user(access_token)
        return identity_proof_for(user)

    @mcp.tool()
    async def list_orders(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> list[Order]:
        """List orders owned by the signed-in user."""
        user = await current_user(access_token)
        return list_orders_for(user.id)

    @mcp.tool()
    async def get_order(
        order_id: OrderId,
        access_token: AccessToken = CurrentAccessToken(),
    ) -> Order:
        """Get one order owned by the signed-in user."""
        user = await current_user(access_token)
        return get_order_for(user.id, order_id)

    @mcp.tool()
    async def create_order(
        items: Annotated[list[OrderItem], Field(min_length=1, max_length=50)],
        access_token: AccessToken = CurrentAccessToken(),
    ) -> Order:
        """Create an order for the signed-in user."""
        user = await current_user(access_token)
        return create_order_for(user.id, user.display_name, items)

    @mcp.tool()
    async def update_order_status(
        order_id: OrderId,
        status: OrderStatus,
        access_token: AccessToken = CurrentAccessToken(),
    ) -> Order:
        """Move an owned order to its next valid status."""
        user = await current_user(access_token)
        return update_order_status_for(user.id, order_id, status)

    # The optional half of the server, decided here rather than inside the
    # module so that the conditionality is visible at one call site. Absent
    # DATABRICKS_HOST there is no workspace and the accelerator runs exactly as
    # it did before, with the Graph tools only.
    databricks_settings = databricks_tools.DatabricksSettings.from_env()
    if databricks_settings is not None:
        databricks_tools.register(mcp, exchange, databricks_settings)

    return mcp


if __name__ == "__main__":
    create_server().run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        show_banner=False,
    )
