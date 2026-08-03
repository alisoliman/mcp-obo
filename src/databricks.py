"""Databricks tools reached through the same delegated token as the Graph ones.

Nothing here decides who may see what. That is the entire point: the SQL below
is a fixed literal, the only variable parts are bound parameters, and the token
carried into Databricks is the caller's. Unity Catalog applies the row filter,
the column mask, and the table grants, and Databricks writes the caller's name
-- not this server's -- into its audit log.

`src/orders.py` is the same identity path with the decision left inside the
process. Compare the two `register` functions: they differ in the scope they ask
Entra for, and in who decides what comes back.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import CurrentAccessToken
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.obo import DATABRICKS_SCOPE, OboExchange

# Unity Catalog identifiers are operator-supplied, never model-supplied, but
# they are still interpolated into SQL text, so they are held to the characters
# Databricks allows in an unquoted identifier. `str.isalnum` is not a substitute:
# it accepts non-ASCII letters and digits such as "½".
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,128}$")

# The single source of truth for these tool names. `src/chat.py` holds the
# equivalent ORDER_TOOLS, and the tests import both rather than restating them,
# so a tool renamed on one side cannot silently drift from the other.
DATABRICKS_TOOLS = frozenset(
    {"databricks_whoami", "databricks_list_tables", "databricks_query_orders"}
)

Region = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9 _-]+$",
    ),
]

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class DatabricksIdentity(BaseModel):
    """Who Databricks believes is asking, by two independent routes."""

    model_config = ConfigDict(frozen=True)

    scim_user_name: str
    scim_display_name: str | None = None
    sql_current_user: str
    workspace_host: str
    evidence: str = "Databricks SCIM /Me and SQL current_user() via OBO"


class TableSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str
    table_type: str | None = None
    comment: str | None = None


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]
    row_count: int
    ran_as: str | None = None
    statement: str


@dataclass(frozen=True)
class DatabricksSettings:
    host: str
    warehouse_id: str
    catalog: str
    schema_name: str

    @classmethod
    def from_env(cls) -> DatabricksSettings | None:
        host = os.getenv("DATABRICKS_HOST", "").strip().rstrip("/")
        if not host:
            return None

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required when DATABRICKS_HOST is set")
            return value

        settings = cls(
            host=host,
            warehouse_id=required("DATABRICKS_WAREHOUSE_ID"),
            catalog=required("DATABRICKS_CATALOG"),
            schema_name=required("DATABRICKS_SCHEMA"),
        )
        # Validated here rather than by the caller, so a DatabricksSettings
        # cannot exist in an unchecked state.
        for name, value in (
            ("DATABRICKS_CATALOG", settings.catalog),
            ("DATABRICKS_SCHEMA", settings.schema_name),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise RuntimeError(f"{name} is not a valid Unity Catalog identifier")
        return settings

    @property
    def orders_table(self) -> str:
        return f"{self.catalog}.{self.schema_name}.orders"


async def _request(
    client: httpx.AsyncClient,
    settings: DatabricksSettings,
    token: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # The client is shared and holds no credential — the token rides in a
    # per-request header — so reusing it keeps the TLS connection warm without
    # any risk of one caller's identity leaking into another's request.
    response = await client.request(
        method,
        f"{settings.host}{path}",
        params=params,
        json=json,
        headers={"Authorization": f"Bearer {token}"},
    )

    if response.status_code >= 400:
        # Surfaced deliberately. A Unity Catalog denial is the demonstration,
        # not an incident, so the caller is told what Databricks decided.
        # Databricks error bodies carry no token material.
        detail = response.text[:400]
        raise ToolError(f"Databricks returned {response.status_code}: {detail}")
    return response.json()


async def _run_statement(
    client: httpx.AsyncClient,
    settings: DatabricksSettings,
    token: str,
    statement: str,
    parameters: list[dict[str, Any]] | None = None,
    row_limit: int = 100,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "warehouse_id": settings.warehouse_id,
        "statement": statement,
        "wait_timeout": "30s",
        "on_wait_timeout": "CANCEL",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
        "row_limit": row_limit,
    }
    if parameters:
        payload["parameters"] = parameters

    body = await _request(
        client, settings, token, "POST", "/api/2.0/sql/statements", json=payload
    )
    state = body.get("status", {}).get("state")
    if state != "SUCCEEDED":
        message = body.get("status", {}).get("error", {}).get("message", state)
        raise ToolError(f"Databricks statement did not succeed ({state}): {message}")
    return body


def _rows_from(body: dict[str, Any]) -> tuple[list[str], list[list[str | None]]]:
    columns = [
        column["name"]
        for column in body.get("manifest", {}).get("schema", {}).get("columns", [])
    ]
    rows = body.get("result", {}).get("data_array", []) or []
    return columns, rows


def orders_statement(table: str) -> str:
    """The only statement this server will ever run against the orders table.

    Fixed text. The caller influences it through bound parameters and nothing
    else, so there is no arrangement of tool arguments that reshapes the query.
    Even if there were, the row filter would still apply.
    """
    return (
        "SELECT current_user() AS ran_as, order_id, owner_email, region, "
        f"customer_email, amount_cents FROM {table} "
        "WHERE (:region IS NULL OR region = :region) "
        "ORDER BY order_id LIMIT :row_limit"
    )


def orders_parameters(region: str | None, limit: int) -> list[dict[str, Any]]:
    """Bind values for `orders_statement`. A NULL parameter carries no value."""
    region_parameter: dict[str, Any] = {"name": "region", "type": "STRING"}
    if region is not None:
        region_parameter["value"] = region
    return [
        region_parameter,
        {"name": "row_limit", "type": "INT", "value": str(limit)},
    ]


def register(
    mcp: FastMCP, exchange: OboExchange, settings: DatabricksSettings
) -> None:
    """Attach the Databricks tools to an already-authenticated MCP server."""

    # One client for the process. It carries no credential, so sharing it costs
    # nothing in isolation and saves a TLS handshake plus an SSL context build
    # on every tool call.
    client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def databricks_token(access_token: AccessToken) -> str:
        return await exchange.token(access_token.token, DATABRICKS_SCOPE)

    @mcp.tool()
    async def databricks_whoami(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> DatabricksIdentity:
        """Show which identity Databricks resolved for the signed-in user."""
        token = await databricks_token(access_token)
        # SCIM answers from the control plane and current_user() answers from
        # the SQL warehouse. Agreeing means the delegated identity survived both
        # hops, not just the first. Neither depends on the other, so they run
        # together.
        me, body = await asyncio.gather(
            _request(client, settings, token, "GET", "/api/2.0/preview/scim/v2/Me"),
            _run_statement(client, settings, token, "SELECT current_user()"),
        )
        _, rows = _rows_from(body)
        return DatabricksIdentity(
            scim_user_name=me.get("userName", ""),
            scim_display_name=me.get("displayName"),
            sql_current_user=rows[0][0] if rows and rows[0] else "",
            workspace_host=settings.host,
        )

    @mcp.tool()
    async def databricks_list_tables(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> list[TableSummary]:
        """List the demo schema's tables the signed-in user may see."""
        token = await databricks_token(access_token)
        body = await _request(
            client,
            settings,
            token,
            "GET",
            "/api/2.1/unity-catalog/tables",
            params={
                "catalog_name": settings.catalog,
                "schema_name": settings.schema_name,
                "max_results": 50,
            },
        )
        return [
            TableSummary(
                full_name=table.get("full_name", ""),
                table_type=table.get("table_type"),
                comment=table.get("comment"),
            )
            for table in body.get("tables", [])
        ]

    @mcp.tool()
    async def databricks_query_orders(
        region: Region | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        access_token: AccessToken = CurrentAccessToken(),
    ) -> QueryResult:
        """Query the Unity Catalog orders table as the signed-in user.

        Unity Catalog applies the row filter and column mask, so the rows this
        returns are already scoped to the caller before the server sees them.
        """
        token = await databricks_token(access_token)
        statement = orders_statement(settings.orders_table)
        body = await _run_statement(
            client,
            settings,
            token,
            statement,
            orders_parameters(region, limit),
            row_limit=limit,
        )
        columns, rows = _rows_from(body)

        # `ran_as` rides along on every row so a result is self-proving, but it
        # is lifted out of the tabular data rather than repeated to the reader.
        ran_as = rows[0][0] if rows and rows[0] else None
        return QueryResult(
            columns=tuple(columns[1:]),
            rows=tuple(tuple(row[1:]) for row in rows),
            row_count=len(rows),
            ran_as=ran_as,
            statement=statement,
        )
