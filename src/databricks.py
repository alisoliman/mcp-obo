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
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import CurrentAccessToken
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.obo import DATABRICKS_SCOPE, OboError, OboExchange

logger = logging.getLogger(__name__)

# Unity Catalog identifiers are operator-supplied, never model-supplied, but
# they are still interpolated into SQL text, so they are held to the characters
# Databricks allows in an unquoted identifier. `str.isalnum` is not a substitute:
# it accepts non-ASCII letters and digits such as "½".
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,128}$")

# The domains Azure Databricks actually serves workspaces from: commercial
# first, Azure Government second. Nothing else is a place a caller's delegated
# token may be sent.
HOST_SUFFIXES = (".azuredatabricks.net", ".databricks.azure.us")

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

# The read timeout has to outlast the `wait_timeout` sent with a statement, or
# the client abandons a request the warehouse is still honouring.
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Every statement this server runs lands in the workspace audit log beside ones
# a human typed. A distinctive User-Agent is what lets an operator tell those
# apart there without guessing.
_USER_AGENT = "mcp-obo-accelerator/0.1.0"

# Databricks accepts 0 or 5-50 seconds here. Thirty leaves the common case --
# a warm warehouse -- answered by the POST itself, with no poll at all.
_WAIT_SECONDS = 30
_STATEMENT_BUDGET = 90.0
_POLL_FIRST = 1.0
_POLL_CAP = 5.0
_PENDING_STATES = frozenset({"PENDING", "RUNNING"})

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0

_PAGE_SIZE = 50
_MAX_PAGES = 20

_MAX_DETAIL = 400
_MAX_NAME = 400
_MAX_COMMENT = 240
_MAX_LABEL = 64

# Statement ids are polled by being put back into a URL path, so they are held
# to the shape Databricks documents before they get there.
_STATEMENT_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Everything Databricks hands back as free text -- a table comment, an error
# message, a display name -- goes through `_flatten` first. A control character
# is what lets one line of third-party text forge a second record in whatever
# log pipeline an adopter attaches (CWE-117), and a newline is what lets a table
# comment look like a new turn in a conversation.
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def _flatten(text: str, limit: int) -> str:
    collapsed = " ".join(_CONTROL.sub(" ", text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _text(value: Any, limit: int) -> str | None:
    """Flatten an optional free-text field, keeping absent as absent."""
    return _flatten(str(value), limit) if value else None


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


class TableListing(BaseModel):
    """The tables Unity Catalog let this caller see, and whether that was all.

    `more_available` exists because Databricks pages this endpoint and documents
    that a page may come back empty while still carrying a next page token. An
    empty list therefore means either "you may see nothing" or "keep asking",
    and telling those two apart is the entire subject of this demo, so the model
    is handed the difference instead of being left to infer it.
    """

    model_config = ConfigDict(frozen=True)

    tables: tuple[TableSummary, ...]
    more_available: bool = False


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]
    row_count: int
    truncated: bool = False
    ran_as: str | None = None
    statement: str
    statement_id: str | None = None


def _validated_host(raw: str) -> str:
    """Normalize DATABRICKS_HOST, or refuse to start.

    This is the one setting that decides where every caller's delegated
    Databricks token is sent, and it was the only one arriving unchecked.
    infra/main.bicep emits it from ARM, so this guards operator error rather
    than an attacker, but two spellings a hand-edited env can hold are total and
    silent: `http://` ships the token in cleartext, and a workspace that
    redirects to TLS does not save you because httpx keeps the Authorization
    header across a same-host redirect and the first hop already carried it;
    a trailing `#` swallows the whole API path into a fragment and leaves every
    request pointing at the bare host.
    """
    try:
        url = httpx.URL(raw)
        host = url.host
    except (httpx.InvalidURL, UnicodeError) as error:
        # UnicodeError catches idna.IDNAError, which is what a malformed
        # punycode label raises on attribute access rather than on parse. Both
        # mean the same thing to an operator and both should read as a refused
        # setting rather than a traceback at startup.
        raise RuntimeError(f"DATABRICKS_HOST is not a URL: {error}") from error

    if url.scheme != "https":
        raise RuntimeError(
            "DATABRICKS_HOST must be https:// -- it is where every caller's "
            "delegated token is sent"
        )
    if not host.endswith(HOST_SUFFIXES):
        raise RuntimeError(
            "DATABRICKS_HOST must be an Azure Databricks workspace URL ending "
            f"in one of {', '.join(HOST_SUFFIXES)}"
        )
    if url.path not in ("", "/") or url.query or url.fragment or url.userinfo:
        raise RuntimeError(
            "DATABRICKS_HOST must be a bare workspace URL, with no path, query, "
            "fragment or embedded credentials"
        )
    return f"https://{url.netloc.decode('ascii')}"


@dataclass(frozen=True)
class DatabricksSettings:
    host: str
    warehouse_id: str
    catalog: str
    schema_name: str

    @classmethod
    def from_env(cls) -> DatabricksSettings | None:
        raw_host = os.getenv("DATABRICKS_HOST", "").strip()
        if not raw_host:
            return None

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required when DATABRICKS_HOST is set")
            return value

        settings = cls(
            host=_validated_host(raw_host),
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
            if not IDENTIFIER.fullmatch(value):
                raise RuntimeError(f"{name} is not a valid Unity Catalog identifier")
        return settings

    @property
    def orders_table(self) -> str:
        return f"{self.catalog}.{self.schema_name}.orders"

    def url(self, path: str) -> httpx.URL:
        """An API URL built from the parsed host rather than formatted onto it.

        `host` has already been reduced to scheme and authority, and joining
        keeps it that way: no path this module passes can move the request to
        another origin, and none can end up as a fragment.
        """
        return httpx.URL(self.host).join(path)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds Databricks asked us to wait, when it asked in seconds.

    The HTTP-date form is legal and Databricks does not use it; treating it as
    absent falls back to the backoff below rather than parsing a date to no end.
    """
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return max(0.0, min(seconds, _BACKOFF_CAP))


def _retry_delay(attempt: int, retry_after: float | None, jitter: float) -> float:
    """Seconds to wait before the next attempt. Pure, so the curve is testable.

    Full jitter, because two pods retrying the same 429 in lockstep is how a
    transient control-plane blip becomes a sustained one.
    """
    if retry_after is not None:
        return retry_after
    return min(_BACKOFF_BASE * 2**attempt, _BACKOFF_CAP) * jitter


def _error_detail(response: httpx.Response) -> str:
    """What Databricks said went wrong, in one line.

    Surfaced deliberately: a Unity Catalog denial is the demonstration, not an
    incident, so the caller is told what Databricks decided. What leaves here is
    the structured `error_code` and `message` when the body carries them, else
    the raw text -- either way flattened to a single capped line, because a
    response body is third-party bytes and both pods log this string.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("error_code") or "")
        message = str(body.get("message") or body.get("error") or "")
        detail = f"{code} {message}".strip() if code else message
        if detail:
            return _flatten(detail, _MAX_DETAIL)
    return _flatten(response.text, _MAX_DETAIL)


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
    url = settings.url(path)
    headers = {"Authorization": f"Bearer {token}"}
    attempt = 0
    while True:
        attempt += 1
        response = await client.request(
            method, url, params=params, json=json, headers=headers
        )
        if response.status_code < 400:
            return response.json()

        # 401 and 403 are deliberately absent from the retryable set: an
        # authorization denial is the demonstration, and retrying it would both
        # delay it and blur it into a transient fault. Everything this module
        # sends is a read, so replaying one cannot double an effect.
        if response.status_code not in _RETRYABLE_STATUS or attempt >= _MAX_ATTEMPTS:
            raise ToolError(
                f"Databricks returned {response.status_code}: "
                f"{_error_detail(response)}"
            )
        await asyncio.sleep(
            _retry_delay(attempt, _retry_after(response), random.random())
        )


def _state(body: dict[str, Any]) -> str:
    return str(body.get("status", {}).get("state", ""))


async def _cancel(
    client: httpx.AsyncClient,
    settings: DatabricksSettings,
    token: str,
    statement_id: str,
) -> None:
    """Hand a statement back rather than walking away from it.

    `POST .../cancel` is the operation the Statement Execution API actually
    exposes; there is no DELETE on a statement. Getting that wrong is silent,
    because a 4xx is a perfectly good HTTP response, so the status is checked
    rather than assumed -- and it matters here, since `on_wait_timeout` is
    CONTINUE and nothing else will stop the statement.

    Deliberately not routed through `_request`: this runs when the caller is
    already being told the real story, and a failed cancel must not replace it.
    """
    if not statement_id:
        return
    try:
        response = await client.request(
            "POST",
            settings.url(f"/api/2.0/sql/statements/{statement_id}/cancel"),
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as error:
        logger.warning(
            "databricks cancel failed statement=%s error=%s",
            statement_id,
            type(error).__name__,
        )
        return
    if response.status_code >= 400:
        logger.warning(
            "databricks cancel refused statement=%s status=%s",
            statement_id,
            response.status_code,
        )


async def _run_statement(
    client: httpx.AsyncClient,
    settings: DatabricksSettings,
    token: str,
    statement: str,
    parameters: list[dict[str, Any]] | None = None,
    row_limit: int = 100,
) -> dict[str, Any]:
    """Run one statement to completion, or give it back at the deadline.

    `on_wait_timeout` is CONTINUE rather than CANCEL because a warehouse that
    has hit its ten-minute auto-stop takes minutes to come back -- longer still
    for the classic PRO warehouse the bootstrap falls back to where serverless
    is unavailable, which provisions a real VM. CANCEL turned that into
    "CANCELED" with no reason attached, every time anyone was first to ask after
    an idle gap. Polling waits that out where the budget allows, and where it
    does not it says plainly that the warehouse is starting rather than letting
    a wait masquerade as a denial.
    """
    payload: dict[str, Any] = {
        "warehouse_id": settings.warehouse_id,
        "statement": statement,
        "wait_timeout": f"{_WAIT_SECONDS}s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
        "row_limit": row_limit,
    }
    if parameters:
        payload["parameters"] = parameters

    deadline = time.monotonic() + _STATEMENT_BUDGET
    body = await _request(
        client, settings, token, "POST", "/api/2.0/sql/statements", json=payload
    )
    statement_id = str(body.get("statement_id") or "")
    if not _STATEMENT_ID.fullmatch(statement_id):
        statement_id = ""

    delay = _POLL_FIRST
    while _state(body) in _PENDING_STATES:
        # Two different failures, kept apart because they call for different
        # things. A statement still pending with no id to poll is Databricks
        # answering in a shape this code does not know, and no amount of
        # retrying fixes it.
        if not statement_id:
            raise ToolError(
                "Databricks accepted the statement but returned no usable "
                "statement id, so it cannot be polled or cancelled."
            )
        if time.monotonic() >= deadline:
            await _cancel(client, settings, token, statement_id)
            # A distinct message on purpose. This is the one failure here that
            # is neither a permission decision nor a broken query, and a caller
            # who is told to simply try again in a minute will be right.
            raise ToolError(
                "The SQL warehouse has not answered yet, so the statement was "
                f"cancelled after {_STATEMENT_BUDGET:.0f}s. This is not an "
                "authorization decision: a stopped warehouse takes minutes to "
                "start, and this request already started it. Retry shortly."
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, _POLL_CAP)
        body = await _request(
            client,
            settings,
            token,
            "GET",
            f"/api/2.0/sql/statements/{statement_id}",
        )

    state = _state(body)
    if state != "SUCCEEDED":
        message = _text(
            body.get("status", {}).get("error", {}).get("message"), _MAX_DETAIL
        )
        raise ToolError(
            f"Databricks statement did not succeed ({state}): "
            f"{message or 'Databricks gave no reason'}"
        )
    return body


def _rows_from(body: dict[str, Any]) -> tuple[list[str], list[list[str | None]]]:
    columns = [
        column["name"]
        for column in body.get("manifest", {}).get("schema", {}).get("columns", [])
    ]
    rows = body.get("result", {}).get("data_array", []) or []
    return columns, rows


def _table_summary(table: dict[str, Any]) -> TableSummary:
    """One Unity Catalog table, with its free text narrowed to a data field.

    A table COMMENT is written by whoever holds CREATE TABLE on the schema and
    lands verbatim in the model's context. In any shared schema that is an
    instruction channel into an agent holding someone else's delegated token:
    Unity Catalog would have decided correctly and the agent would still have
    been the confused deputy. No attempt is made to detect an injection --
    detection is a losing game -- the channel is only made narrow and dull, one
    capped line with no control characters, so it reads as data. Table names get
    the same treatment because a quoted identifier can hold anything too.
    """
    return TableSummary(
        full_name=_flatten(str(table.get("full_name", "")), _MAX_NAME),
        table_type=_text(table.get("table_type"), _MAX_LABEL),
        comment=_text(table.get("comment"), _MAX_COMMENT),
    )


async def _list_tables(
    client: httpx.AsyncClient, settings: DatabricksSettings, token: str
) -> TableListing:
    """Every table in the demo schema this caller may see, following the pages.

    Databricks documents that a page may hold zero results and still carry a
    next page token, and that the token's absence is the only signal that the
    results have ended. So an empty page is never read as "that is all": doing
    that would be indistinguishable from Unity Catalog denying everything, which
    is the one distinction this accelerator exists to make legible.
    """
    tables: list[TableSummary] = []
    page_token = ""
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {
            "catalog_name": settings.catalog,
            "schema_name": settings.schema_name,
            "max_results": _PAGE_SIZE,
            # Nothing downstream reads column or property metadata, and on a
            # real schema both dwarf the part that is actually shown.
            "omit_columns": True,
            "omit_properties": True,
        }
        if page_token:
            params["page_token"] = page_token
        body = await _request(
            client, settings, token, "GET", "/api/2.1/unity-catalog/tables",
            params=params,
        )
        tables.extend(_table_summary(table) for table in body.get("tables") or [])
        page_token = str(body.get("next_page_token") or "")
        if not page_token:
            break

    # A leftover token means the page cap stopped the walk, not Databricks. The
    # model is told so it cannot report a capped list as the whole schema.
    return TableListing(tables=tuple(tables), more_available=bool(page_token))


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
    client = httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})

    def log_call(tool: str, access_token: AccessToken, statement_id: str = "") -> None:
        """One line per tool call, joinable to Databricks' own audit row.

        `oid` is Entra's object id for the caller: an opaque identifier, not a
        credential. The statement id is what joins this line to
        system.query.history on the Databricks side. The token is not an
        argument here, so it cannot end up in the line by accident.
        """
        oid = access_token.claims.get("oid") or access_token.claims.get("sub") or ""
        logger.info(
            "databricks tool=%s oid=%s statement=%s",
            tool,
            _text(oid, _MAX_LABEL) or "-",
            statement_id or "-",
        )

    async def databricks_token(access_token: AccessToken) -> str:
        try:
            return await exchange.token(access_token.token, DATABRICKS_SCOPE)
        except OboError as error:
            # FastMCP masks every exception except ToolError, so an identity
            # failure a caller could actually act on -- missing consent, a
            # Conditional Access step-up -- only survives the mask as one.
            raise ToolError(str(error)) from error

    @mcp.tool(annotations={"readOnlyHint": True})
    async def databricks_whoami(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> DatabricksIdentity:
        """Show which identity Databricks resolved for the signed-in user."""
        token = await databricks_token(access_token)
        # SCIM answers from the control plane and current_user() answers from
        # the SQL warehouse. Agreeing means the delegated identity survived both
        # hops, not just the first. Neither depends on the other, so they run
        # together.
        #
        # Explicit tasks rather than bare coroutines because `gather` propagates
        # the first failure without stopping its siblings: a SCIM 403 for a
        # caller the workspace does not know returns in milliseconds, and would
        # otherwise leave the SQL leg polling a cold warehouse for the whole
        # statement budget after the tool has already failed.
        me_task = asyncio.ensure_future(
            _request(client, settings, token, "GET", "/api/2.0/preview/scim/v2/Me")
        )
        sql_task = asyncio.ensure_future(
            _run_statement(client, settings, token, "SELECT current_user()")
        )
        try:
            me, body = await asyncio.gather(me_task, sql_task)
        except BaseException:
            for task in (me_task, sql_task):
                task.cancel()
            await asyncio.gather(me_task, sql_task, return_exceptions=True)
            raise
        _, rows = _rows_from(body)
        log_call(
            "databricks_whoami",
            access_token,
            _text(body.get("statement_id"), _MAX_LABEL) or "",
        )
        return DatabricksIdentity(
            scim_user_name=_text(me.get("userName"), _MAX_LABEL) or "",
            scim_display_name=_text(me.get("displayName"), _MAX_LABEL),
            sql_current_user=rows[0][0] if rows and rows[0] else "",
            workspace_host=settings.host,
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def databricks_list_tables(
        access_token: AccessToken = CurrentAccessToken(),
    ) -> TableListing:
        """List the demo schema's tables the signed-in user may see."""
        token = await databricks_token(access_token)
        listing = await _list_tables(client, settings, token)
        log_call("databricks_list_tables", access_token)
        return listing

    @mcp.tool(annotations={"readOnlyHint": True})
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
        statement_id = _text(body.get("statement_id"), _MAX_LABEL) or ""
        log_call("databricks_query_orders", access_token, statement_id)

        # `ran_as` rides along on every row so a result is self-proving, but it
        # is lifted out of the tabular data rather than repeated to the reader.
        ran_as = rows[0][0] if rows and rows[0] else None
        return QueryResult(
            columns=tuple(columns[1:]),
            rows=tuple(tuple(row[1:]) for row in rows),
            row_count=len(rows),
            # `orders_statement` carries its own LIMIT, so this stays false for
            # the demo query. It is read and reported because widening that
            # statement is exactly what docs/databricks-mode.md invites, and a
            # result the warehouse trimmed at `row_limit` is otherwise
            # indistinguishable from a complete one to whoever summarises it.
            truncated=bool(body.get("manifest", {}).get("truncated")),
            ran_as=ran_as,
            statement=statement,
            statement_id=statement_id or None,
        )
