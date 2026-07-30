"""Orders mode: the MCP server authorizes, using Microsoft Graph to say who you are.

This is the starting point for building your own MCP server on Azure. The store
is an in-memory dict, but the identity path around it is the real thing: the
caller's token is exchanged on their behalf for a Microsoft Graph token, Graph
`/me` returns the object id of the human who asked, and that object id is the
only key the store is partitioned by.

Note where the authorization decision lives. `list_orders_for(user.id)` is a
line of Python. It is correct only for as long as every tool remembers to pass
the caller's id and nothing else. That is a fine trade for a service that owns
its own data, and it is the model most MCP servers need.

`src/databricks.py` is the same identity path with the decision moved out of the
process entirely. Compare the two `register` functions: they differ in the scope
they ask Entra for, and in who decides what comes back.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from enum import StrEnum
from itertools import count
from typing import Annotated

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import CurrentAccessToken
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.obo import GRAPH_SCOPE, OboExchange

# The single source of truth for these names. `src/chat.py` passes them to the
# agent as its allow-list and the tests import them, so a tool renamed here
# cannot silently drop out of either.
ORDER_TOOLS = frozenset(
    {"who_am_i", "list_orders", "get_order", "create_order", "update_order_status"}
)

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


def register(mcp: FastMCP, exchange: OboExchange) -> None:
    """Attach the order tools to an already-authenticated MCP server."""

    client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))

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
