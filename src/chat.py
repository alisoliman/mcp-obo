"""Order agent chat: the confidential client in the delegated OBO flow.

The workload's own AKS identity signs the client assertion during the
authorization code flow and authenticates to Microsoft Foundry for inference. It
is never used to call the MCP server.

Every MCP request instead carries the signed-in user's delegated token, taken
from that user's server-side MSAL cache. Tokens stay on the server; the browser
only ever sees a session cookie, a CSRF token, and the sanitized tool events
streamed back over SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from agent_framework import (
    Agent,
    AgentSession,
    FunctionInvocationContext,
    FunctionMiddleware,
    MCPStreamableHTTPTool,
)
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import WorkloadIdentityCredential
from msal import ConfidentialClientApplication, SerializableTokenCache
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.databricks import DATABRICKS_TOOLS
from src.orders import ORDER_TOOLS
from src.server import DATABRICKS_MODE, current_mode


# The `__Host-` prefix makes the browser refuse the cookie unless it was set
# with Secure, Path=/ and no Domain, which is the one control that survives the
# DNS shape this accelerator ships: `cloudapp.azure.com` is not on the Public
# Suffix List (only `cloudapp.net` is), so a sibling Azure DNS label in the same
# region can otherwise set a Domain-scoped cookie of this name on us.
COOKIE_NAME = "__Host-order_agent_session"
# Idle timeout, refreshed on each request, and a hard ceiling measured from the
# moment the user actually signed in. Without the ceiling a stolen cookie stays
# usable for as long as MSAL can keep refreshing, which outlives whatever
# sign-in frequency the tenant's Conditional Access policy asked for.
SESSION_IDLE_TTL_SECONDS = 30 * 60
SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60
# A session that /login created but Entra has not yet returned to only has to
# survive one round trip, and it is the one an unauthenticated caller can mint.
# Ten minutes rather than something tighter because that is how long Entra keeps
# the authorization code alive: expiring first would fail a sign-in that is
# still perfectly valid, which is what a user pausing on an MFA prompt looks
# like.
PRE_AUTH_TTL_SECONDS = 10 * 60
# The store is per-pod and in memory, so an unauthenticated GET /login loop is
# an allocation primitive. This is a ceiling, not capacity planning.
MAX_SESSIONS = 2_000
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIST = ROOT / "frontend" / "dist"
ORDERS_INSTRUCTIONS = (
    "You help the signed-in user with their orders. "
    "Use the order tools for order facts and never invent order data. "
    "Only create an order or change status when the user explicitly asks. "
    "When asked to prove identity or OBO, call who_am_i and report its "
    "display name, identity fingerprint, and evidence exactly."
)
DATABRICKS_INSTRUCTIONS = (
    "You answer questions about the signed-in user's orders, which live in a "
    "Unity Catalog table in Azure Databricks. "
    "Use databricks_query_orders for order data and never invent rows. "
    "Call databricks_whoami when asked which identity Databricks resolved, and "
    "report its results exactly. "
    "Unity Catalog filters rows and masks columns before you see them, so never "
    "present a masked value as if it were real, and when it comes up say plainly "
    "that the filtering happened in Databricks rather than in this application."
)


def allowed_tools() -> frozenset[str]:
    """The tool names for the mode this deployment is running.

    Imported from the tool modules rather than restated, so a tool renamed on
    the server cannot silently drop out of the agent's allow-list.
    """
    return DATABRICKS_TOOLS if current_mode() == DATABRICKS_MODE else ORDER_TOOLS


def agent_instructions() -> str:
    return (
        DATABRICKS_INSTRUCTIONS
        if current_mode() == DATABRICKS_MODE
        else ORDERS_INSTRUCTIONS
    )


logger = logging.getLogger(__name__)
# A substring denylist over key names, applied to every tool result before it
# reaches the browser. Nothing in this repo's tools can return a credential —
# they return frozen models with no such field — so this exists for the tools an
# adopter bolts on. Treat it as a floor: a tool that returns a secret under a
# name nobody thought of is still a leak, and the fix is the tool, not this list.
_SENSITIVE_EVENT_KEYS = (
    "_key",
    "apikey",
    "assertion",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "jwt",
    "passphrase",
    "password",
    "private",
    "sas",
    "secret",
    "session",
    "signature",
    "token",
)


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    client_id: str
    redirect_uri: str
    app_url: str
    mcp_scope: str
    mcp_endpoint: str
    foundry_project_endpoint: str
    foundry_model: str
    token_file: Path

    @classmethod
    def from_env(cls) -> Settings:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required")
            return value

        return cls(
            tenant_id=required("AZURE_TENANT_ID"),
            client_id=required("ENTRA_CLIENT_CLIENT_ID"),
            redirect_uri=required("OAUTH_REDIRECT_URI"),
            app_url=required("APP_URL").rstrip("/"),
            mcp_scope=required("MCP_SCOPE"),
            mcp_endpoint=required("MCP_ENDPOINT"),
            foundry_project_endpoint=required("FOUNDRY_PROJECT_ENDPOINT"),
            foundry_model=required("FOUNDRY_MODEL_DEPLOYMENT_NAME"),
            token_file=Path(
                os.getenv(
                    "AZURE_FEDERATED_TOKEN_FILE",
                    "/var/run/secrets/azure/tokens/azure-identity-token",
                )
            ),
        )


@dataclass
class BrowserSession:
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    token_cache: str = ""
    auth_flow: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    agent_session: AgentSession | None = None
    # None until the authorization code has been redeemed. Both the absolute
    # ceiling and the pre-authentication TTL key off this, so it is the flag for
    # "this session has been through Entra" as much as it is a timestamp.
    authenticated_at: float | None = None
    expires_at: float = field(
        default_factory=lambda: time.time() + PRE_AUTH_TTL_SECONDS
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def authenticate(self, now: float) -> None:
        self.authenticated_at = now
        self.expires_at = now + SESSION_IDLE_TTL_SECONDS

    def touch(self, now: float) -> None:
        """Slide the idle deadline, but never past the absolute one.

        A pre-authentication session is deliberately not slid: it has one job,
        and letting an unauthenticated caller keep it alive by polling would
        hand back the unbounded lifetime this is here to remove.
        """
        if self.authenticated_at is None:
            return
        self.expires_at = min(
            now + SESSION_IDLE_TTL_SECONDS,
            self.authenticated_at + SESSION_ABSOLUTE_TTL_SECONDS,
        )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


def _safe_event_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[truncated]"
    if isinstance(value, BaseModel):
        return _safe_event_value(value.model_dump(mode="json"), depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if any(term in str(key).lower() for term in _SENSITIVE_EVENT_KEYS)
                else _safe_event_value(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_event_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 4_000 else f"{value[:4_000]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_event_value(str(value), depth=depth + 1)


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(
        _safe_event_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


def _sse_text_event(event: str, text: str) -> str:
    data = json.dumps(
        {"text": text},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


def _tool_result_value(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    text_values = [
        item.text
        for item in value
        if getattr(item, "type", None) == "text" and getattr(item, "text", None)
    ]
    if not text_values:
        return value
    for text in text_values:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return text_values[0] if len(text_values) == 1 else text_values


class ToolEventMiddleware(FunctionMiddleware):
    def __init__(self, events: asyncio.Queue) -> None:
        self.events = events

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        event_id = secrets.token_urlsafe(9)
        await self.events.put(
            (
                "tool.started",
                {
                    "id": event_id,
                    "name": context.function.name,
                    "input": context.arguments,
                },
            )
        )
        try:
            await call_next()
        except Exception:
            await self.events.put(
                (
                    "tool.completed",
                    {
                        "id": event_id,
                        "name": context.function.name,
                        "status": "error",
                        "output": "Tool call failed.",
                    },
                )
            )
            raise
        await self.events.put(
            (
                "tool.completed",
                {
                    "id": event_id,
                    "name": context.function.name,
                    "status": "completed",
                    "input": context.arguments,
                    "output": _tool_result_value(context.result),
                },
            )
        )


def _workload_assertion(settings: Settings) -> str:
    assertion = settings.token_file.read_text(encoding="utf-8").strip()
    if not assertion:
        raise RuntimeError("The projected workload identity token is empty")
    return assertion


def _msal_app(
    settings: Settings,
    session: BrowserSession,
) -> tuple[ConfidentialClientApplication, SerializableTokenCache]:
    cache = SerializableTokenCache()
    if session.token_cache:
        cache.deserialize(session.token_cache)
    app = ConfidentialClientApplication(
        settings.client_id,
        authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
        client_credential={
            "client_assertion": lambda: _workload_assertion(settings),
        },
        token_cache=cache,
    )
    return app, cache


def _save_cache(session: BrowserSession, cache: SerializableTokenCache) -> None:
    if cache.has_state_changed:
        session.token_cache = cache.serialize()


def _cookie(response: Response, session_id: str, *, max_age: int) -> None:
    # Entra returns the authorization code as a cross-site form POST, which a
    # SameSite=Lax cookie would not accompany. Every state-changing route checks
    # the CSRF header instead of relying on SameSite. Secure and Path=/ with no
    # Domain are what the `__Host-` prefix requires; a browser that sees the
    # prefix without them drops the cookie entirely.
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )


def _clear_cookie(response: Response) -> None:
    # The deletion cookie has to satisfy the `__Host-` rules too, otherwise the
    # browser rejects it and the session cookie survives the logout.
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )


_SECURITY_HEADERS = {
    # style-src has to allow inline styles: Radix's dropdown pulls in
    # react-remove-scroll, which injects a <style> element at runtime to lock
    # body scroll, and a static bundle has no way to carry a per-response nonce.
    # Everything else is closed. img-src is the one that blocks the prompt
    # injection exfiltration channel — table COMMENT text from Unity Catalog
    # reaches the model, and a remote <img> in the model's markdown would be a
    # silent GET carrying whatever it was told to encode.
    #
    # frame-ancestors is load-bearing rather than routine. The session cookie
    # must stay SameSite=none for Entra's cross-site form POST, so it rides
    # along inside a hostile iframe; without frame-ancestors an attacker page
    # can frame the signed-in UI, with its CSRF token already fetched, and
    # drive a real turn by redressing the send button.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """Stamps the response headers on every route, including the SSE stream.

    Raw ASGI rather than BaseHTTPMiddleware on purpose: BaseHTTPMiddleware pumps
    every body chunk through an anyio stream, which would put a queue between
    the agent's token deltas and the browser. This only rewrites the
    `http.response.start` message and never touches the body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(
    settings: Settings | None = None,
    *,
    frontend_dist: Path | None = None,
) -> Starlette:
    config = settings or Settings.from_env()
    static_root = frontend_dist or DEFAULT_FRONTEND_DIST
    index_path = static_root / "index.html"
    assets_path = static_root / "assets"
    if not index_path.is_file() or not assets_path.is_dir():
        raise RuntimeError(
            f"Frontend build is missing from {static_root}. Run npm --prefix frontend run build."
        )
    sessions: dict[str, BrowserSession] = {}

    def store_session(session_id: str, session: BrowserSession, now: float) -> None:
        """Insert, sweeping the expired and capping what is left.

        Sweeping on insert is enough because insert is the only unauthenticated
        path into this dict; evicting the soonest-to-expire first drops the
        pre-authentication shells a flood creates, which carry the shortest TTL
        of anything here, before it touches anyone who actually signed in.
        """
        for stale_id in [sid for sid, s in sessions.items() if s.expires_at <= now]:
            sessions.pop(stale_id, None)
        while len(sessions) >= MAX_SESSIONS:
            sessions.pop(min(sessions, key=lambda sid: sessions[sid].expires_at), None)
        sessions[session_id] = session

    def current_session(request: Request) -> tuple[str, BrowserSession] | None:
        session_id = request.cookies.get(COOKIE_NAME)
        if not session_id:
            return None
        session = sessions.get(session_id)
        now = time.time()
        if session is None or session.expires_at <= now:
            sessions.pop(session_id, None)
            return None
        session.touch(now)
        return session_id, session

    def require_csrf(request: Request, session: BrowserSession) -> bool:
        supplied = request.headers.get("X-CSRF-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, session.csrf_token)

    async def index(_: Request) -> FileResponse:
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})

    async def favicon(_: Request) -> FileResponse:
        return FileResponse(static_root / "favicon.svg", media_type="image/svg+xml")

    async def session_info(request: Request) -> JSONResponse:
        payload: dict[str, Any] = {
            "authenticated": False,
            "model": config.foundry_model,
            # The browser needs the mode to name the downstream resource
            # truthfully; Graph is never contacted in databricks mode.
            "mode": current_mode(),
            "mcp": {"tools": sorted(allowed_tools())},
        }
        active = current_session(request)
        if active is None or active[1].profile is None:
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})

        _, session = active
        claims = session.profile or {}
        username = str(
            claims.get("preferred_username") or claims.get("email") or ""
        )
        payload.update(
            authenticated=True,
            profile={
                "name": str(claims.get("name") or "Signed-in user"),
                **({"username": username} if username else {}),
            },
            csrfToken=session.csrf_token,
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def login(_: Request) -> RedirectResponse:
        session_id = secrets.token_urlsafe(32)
        session = BrowserSession()
        store_session(session_id, session, time.time())
        app, _ = _msal_app(config, session)
        flow = await asyncio.to_thread(
            app.initiate_auth_code_flow,
            scopes=[config.mcp_scope],
            redirect_uri=config.redirect_uri,
            response_mode="form_post",
        )
        session.auth_flow = flow
        response = RedirectResponse(str(flow["auth_uri"]), status_code=302)
        _cookie(response, session_id, max_age=PRE_AUTH_TTL_SECONDS)
        return response

    async def auth_callback(request: Request) -> Response:
        active = current_session(request)
        if active is None:
            return PlainTextResponse("Authentication session expired.", status_code=400)
        session_id, session = active
        if session.auth_flow is None:
            return PlainTextResponse("Authentication flow is missing.", status_code=400)

        form = dict(await request.form())
        async with session.lock:
            app, cache = _msal_app(config, session)
            try:
                result = await asyncio.to_thread(
                    app.acquire_token_by_auth_code_flow,
                    session.auth_flow,
                    form,
                )
            except ValueError:
                return PlainTextResponse("Authentication state is invalid.", status_code=400)
            finally:
                session.auth_flow = None

            if "error" in result:
                detail = str(result.get("error_description", result["error"]))
                return PlainTextResponse(
                    f"Authentication failed: {detail}",
                    status_code=400,
                )

            session.profile = dict(result.get("id_token_claims") or {})
            _save_cache(session, cache)

            # The privilege change is here, so the identifier changes here. An
            # id that reached this pod in a planted cookie now names nothing,
            # and the attacker's copy cannot follow the user in. This is not
            # theoretical for the DNS shape the README deploys:
            # `cloudapp.azure.com` is absent from the Public Suffix List, so a
            # sibling label in the same region can set a Domain-scoped cookie
            # here, and SameSite=none means it rides Entra's form POST.
            # Everything downstream — the MCP server, the OBO exchange, Unity
            # Catalog — would correctly believe the attacker is this user,
            # because this is the only layer that decides who asked.
            sessions.pop(session_id, None)
            session_id = secrets.token_urlsafe(32)
            session.csrf_token = secrets.token_urlsafe(32)
            now = time.time()
            session.authenticate(now)
            store_session(session_id, session, now)

        response = RedirectResponse("/", status_code=303)
        # The cookie carries the absolute lifetime; the idle timeout is the
        # server's business and is not something the browser should enforce.
        _cookie(response, session_id, max_age=SESSION_ABSOLUTE_TTL_SECONDS)
        return response

    async def delegated_token(session: BrowserSession) -> str | None:
        app, cache = _msal_app(config, session)
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = await asyncio.to_thread(
            app.acquire_token_silent,
            [config.mcp_scope],
            account=accounts[0],
        )
        _save_cache(session, cache)
        if not result or "access_token" not in result:
            return None
        return str(result["access_token"])

    async def chat_stream(request: Request) -> Response:
        active = current_session(request)
        if active is None:
            return JSONResponse({"error": "Sign-in required"}, status_code=401)
        session_id, session = active
        if session.profile is None:
            return JSONResponse({"error": "Sign-in required"}, status_code=401)
        if not require_csrf(request, session):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)

        try:
            payload = ChatRequest.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError):
            return JSONResponse({"error": "A message of 1-4000 characters is required"}, status_code=400)

        async def event_stream():
            stream_task: asyncio.Task | None = None
            await session.lock.acquire()
            try:
                token = await delegated_token(session)
                if token is None:
                    yield _sse_event(
                        "response.error",
                        {
                            "code": "auth_required",
                            "message": "Sign-in expired.",
                        },
                    )
                    return

                yield _sse_event("response.started", {})
                tool_events: asyncio.Queue = asyncio.Queue()
                tool_middleware = ToolEventMiddleware(tool_events)
                async with (
                    WorkloadIdentityCredential(
                        tenant_id=config.tenant_id,
                        client_id=config.client_id,
                        token_file_path=str(config.token_file),
                    ) as credential,
                    httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=httpx.Timeout(30, read=300),
                        follow_redirects=True,
                    ) as mcp_http,
                ):
                    mcp = MCPStreamableHTTPTool(
                        name="orders",
                        description="Order tools for the signed-in user.",
                        url=config.mcp_endpoint,
                        allowed_tools=allowed_tools(),
                        # Deliberate for the demo, and a real limit. Answering
                        # an approval prompt needs a browser-to-server channel
                        # this one-way SSE stream does not have, so a gate here
                        # would hang the turn rather than ask. The databricks
                        # tools only read; the order tools do not, and
                        # create_order and update_order_status run on arguments
                        # a model chose after reading text this app does not
                        # control. Anyone taking this past a demo has to build
                        # that channel before trusting a mutating tool.
                        approval_mode="never_require",
                        load_prompts=False,
                        http_client=mcp_http,
                    )
                    agent = Agent(
                        client=FoundryChatClient(
                            project_endpoint=config.foundry_project_endpoint,
                            model=config.foundry_model,
                            credential=credential,
                        ),
                        name="OrderAgent",
                        instructions=agent_instructions(),
                        tools=[mcp],
                    )
                    async with agent:
                        if session.agent_session is None:
                            session.agent_session = agent.create_session(
                                session_id=session_id
                            )
                        response_stream = agent.run(
                            payload.message,
                            session=session.agent_session,
                            stream=True,
                            middleware=[tool_middleware],
                        )

                        async def consume_agent_stream() -> None:
                            try:
                                async for update in response_stream:
                                    for content in update.contents:
                                        content_type = str(content.type)
                                        if content_type == "text" and content.text:
                                            await tool_events.put(
                                                (
                                                    "response.delta",
                                                    {"text": content.text},
                                                )
                                            )
                                        elif content_type == "error":
                                            await tool_events.put(
                                                (
                                                    "response.error",
                                                    {
                                                        "message": (
                                                            "The agent request failed."
                                                        )
                                                    },
                                                )
                                            )
                                            return
                                final = await response_stream.get_final_response()
                                await tool_events.put(
                                    (
                                        "response.completed",
                                        {"text": final.text},
                                    )
                                )
                            except Exception as exc:
                                await tool_events.put(("stream.exception", exc))
                            finally:
                                await tool_events.put(("stream.done", None))

                        stream_task = asyncio.create_task(consume_agent_stream())
                        while True:
                            event_name, event_payload = await tool_events.get()
                            if event_name == "stream.done":
                                break
                            if event_name == "stream.exception":
                                raise event_payload
                            if event_name in {
                                "response.delta",
                                "response.completed",
                            }:
                                yield _sse_text_event(
                                    event_name,
                                    event_payload["text"],
                                )
                            else:
                                yield _sse_event(event_name, event_payload)
            except Exception:
                logger.exception("Streaming agent request failed")
                yield _sse_event(
                    "response.error",
                    {"message": "The agent request failed."},
                )
            finally:
                if stream_task is not None and not stream_task.done():
                    stream_task.cancel()
                if stream_task is not None:
                    await asyncio.gather(stream_task, return_exceptions=True)
                session.lock.release()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def logout(request: Request) -> JSONResponse:
        active = current_session(request)
        if active is None:
            return JSONResponse({"logoutUrl": "/"})
        session_id, session = active
        if not require_csrf(request, session):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
        sessions.pop(session_id, None)
        logout_url = (
            f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/logout?"
            + urlencode({"post_logout_redirect_uri": config.app_url})
        )
        response = JSONResponse({"logoutUrl": logout_url})
        _clear_cookie(response)
        return response

    return Starlette(
        debug=False,
        middleware=[Middleware(SecurityHeadersMiddleware)],
        routes=[
            Route("/", index),
            Route("/favicon.svg", favicon),
            Route("/health", health),
            Route("/login", login),
            Route("/auth/callback", auth_callback, methods=["POST"]),
            Route("/api/session", session_info),
            Route("/api/chat/stream", chat_stream, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Mount("/assets", app=StaticFiles(directory=assets_path), name="assets"),
        ],
    )


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        access_log=False,
    )
