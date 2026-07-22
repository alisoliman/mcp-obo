from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from enum import StrEnum
from itertools import count
from pathlib import Path
from typing import Annotated

import httpx
from azure.identity.aio import OnBehalfOfCredential
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, RemoteAuthProvider
from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from fastmcp.server.dependencies import CurrentAccessToken
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints

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
        assertion = token_file.read_text(encoding="utf-8").strip()
        if not assertion:
            raise RuntimeError("The projected workload identity token is empty")
        return assertion

    async def current_user(access_token: AccessToken) -> GraphUser:
        # ponytail: exchange per call; cache by assertion only if demo traffic warrants it.
        credential = OnBehalfOfCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_assertion_func=workload_assertion,
            user_assertion=access_token.token,
        )
        try:
            graph_token = await credential.get_token(
                "https://graph.microsoft.com/.default"
            )
        finally:
            await credential.close()

        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                params={"$select": "id,displayName,userPrincipalName"},
                headers={"Authorization": f"Bearer {graph_token.token}"},
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

    return mcp


if __name__ == "__main__":
    create_server().run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        show_banner=False,
    )
