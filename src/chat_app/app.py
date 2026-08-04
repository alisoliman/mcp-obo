"""Order agent chat: the confidential client in the delegated OBO flow.

The workload's own AKS identity signs the client assertion during the
authorization code flow and authenticates to Microsoft Foundry for inference. It
is never used to call the MCP server.

Every MCP request instead carries the signed-in user's delegated token, taken
from that user's server-side MSAL cache. Tokens stay on the server; the browser
only ever sees a session cookie, a CSRF token, and the sanitized tool events
streamed back over SSE.

This module is the HTTP surface only. The pieces it composes:

    settings.py  what the environment supplies
    sessions.py  who is asking, and for how long
    auth.py      the sign-in, and the user's delegated token
    events.py    what the browser is allowed to see of a turn
    security.py  the response headers
    prompts.py   what the agent is told, and which tools it may call
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import WorkloadIdentityCredential
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
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

from src.chat_app.auth import delegated_token, msal_app, save_cache
from src.chat_app.events import (
    ToolEventMiddleware,
    sse_event,
    sse_text_event,
)
from src.chat_app.prompts import agent_instructions, allowed_tools
from src.chat_app.security import SecurityHeadersMiddleware
from src.chat_app.sessions import (
    COOKIE_NAME,
    PRE_AUTH_TTL_SECONDS,
    SESSION_ABSOLUTE_TTL_SECONDS,
    BrowserSession,
    SessionStore,
    clear_session_cookie,
    set_session_cookie,
)
from src.chat_app.settings import DEFAULT_FRONTEND_DIST, Settings
from src.shared.modes import current_mode

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


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
    sessions = SessionStore()

    def current_session(request: Request) -> tuple[str, BrowserSession] | None:
        session_id = request.cookies.get(COOKIE_NAME)
        if not session_id:
            return None
        session = sessions.get(session_id, time.time())
        if session is None:
            return None
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
        username = str(claims.get("preferred_username") or claims.get("email") or "")
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
        sessions.add(session_id, session, time.time())
        app, _ = msal_app(config, session)
        flow = await asyncio.to_thread(
            app.initiate_auth_code_flow,
            scopes=[config.mcp_scope],
            redirect_uri=config.redirect_uri,
            response_mode="form_post",
        )
        session.auth_flow = flow
        response = RedirectResponse(str(flow["auth_uri"]), status_code=302)
        set_session_cookie(response, session_id, max_age=PRE_AUTH_TTL_SECONDS)
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
            app, cache = msal_app(config, session)
            try:
                result = await asyncio.to_thread(
                    app.acquire_token_by_auth_code_flow,
                    session.auth_flow,
                    form,
                )
            except ValueError:
                return PlainTextResponse(
                    "Authentication state is invalid.", status_code=400
                )
            finally:
                session.auth_flow = None

            if "error" in result:
                detail = str(result.get("error_description", result["error"]))
                return PlainTextResponse(
                    f"Authentication failed: {detail}",
                    status_code=400,
                )

            session.profile = dict(result.get("id_token_claims") or {})
            save_cache(session, cache)

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
            sessions.pop(session_id)
            session_id = secrets.token_urlsafe(32)
            session.csrf_token = secrets.token_urlsafe(32)
            now = time.time()
            session.authenticate(now)
            sessions.add(session_id, session, now)

        response = RedirectResponse("/", status_code=303)
        # The cookie carries the absolute lifetime; the idle timeout is the
        # server's business and is not something the browser should enforce.
        set_session_cookie(response, session_id, max_age=SESSION_ABSOLUTE_TTL_SECONDS)
        return response

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
            return JSONResponse(
                {"error": "A message of 1-4000 characters is required"}, status_code=400
            )

        async def event_stream() -> AsyncGenerator[str]:
            stream_task: asyncio.Task | None = None
            await session.lock.acquire()
            try:
                token = await delegated_token(config, session)
                if token is None:
                    yield sse_event(
                        "response.error",
                        {
                            "code": "auth_required",
                            "message": "Sign-in expired.",
                        },
                    )
                    return

                yield sse_event("response.started", {})
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
                                yield sse_text_event(
                                    event_name,
                                    event_payload["text"],
                                )
                            else:
                                yield sse_event(event_name, event_payload)
            except Exception:
                logger.exception("Streaming agent request failed")
                yield sse_event(
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
        sessions.pop(session_id)
        logout_url = (
            f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/logout?"
            + urlencode({"post_logout_redirect_uri": config.app_url})
        )
        response = JSONResponse({"logoutUrl": logout_url})
        clear_session_cookie(response)
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
