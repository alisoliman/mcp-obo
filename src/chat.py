from __future__ import annotations

import asyncio
import html
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from agent_framework import Agent, AgentSession, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import WorkloadIdentityCredential
from msal import ConfidentialClientApplication, SerializableTokenCache
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route


COOKIE_NAME = "order_agent_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
ORDER_TOOLS = {
    "list_orders",
    "get_order",
    "create_order",
    "update_order_status",
}


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
    expires_at: float = field(
        default_factory=lambda: time.time() + SESSION_TTL_SECONDS
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


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


def _cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="none",
    )


def _page(profile: dict[str, Any] | None, csrf_token: str | None = None) -> str:
    if profile is None:
        body = """
        <main>
          <h1>Order agent</h1>
          <p>Sign in to ask about your orders.</p>
          <a class="button" href="/login">Sign in with Microsoft</a>
        </main>
        """
    else:
        name = html.escape(str(profile.get("name") or "Signed-in user"))
        csrf = json.dumps(csrf_token)
        body = f"""
        <main>
          <header>
            <div><h1>Order agent</h1><small>{name}</small></div>
            <button id="logout" class="secondary">Sign out</button>
          </header>
          <section id="messages" aria-live="polite"></section>
          <form id="chat-form">
            <label for="message">Message</label>
            <div class="composer">
              <input id="message" maxlength="4000"
                     placeholder="What orders do I have?" required autofocus>
              <button type="submit">Send</button>
            </div>
          </form>
        </main>
        <script>
          const csrf = {csrf};
          const messages = document.querySelector("#messages");
          const form = document.querySelector("#chat-form");
          const input = document.querySelector("#message");

          function add(role, text) {{
            const item = document.createElement("p");
            item.className = role;
            item.textContent = text;
            messages.appendChild(item);
            messages.scrollTop = messages.scrollHeight;
          }}

          form.addEventListener("submit", async (event) => {{
            event.preventDefault();
            const message = input.value.trim();
            if (!message) return;
            add("user", message);
            input.value = "";
            input.disabled = true;
            try {{
              const response = await fetch("/api/chat", {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                  "X-CSRF-Token": csrf
                }},
                body: JSON.stringify({{message}})
              }});
              const payload = await response.json();
              if (response.status === 401) {{
                location.href = "/login";
                return;
              }}
              if (!response.ok) throw new Error(payload.error || "Chat failed");
              add("agent", payload.response);
            }} catch (error) {{
              add("error", error.message);
            }} finally {{
              input.disabled = false;
              input.focus();
            }}
          }});

          document.querySelector("#logout").addEventListener("click", async () => {{
            const response = await fetch("/logout", {{
              method: "POST",
              headers: {{"X-CSRF-Token": csrf}}
            }});
            location.href = response.redirected ? response.url : "/";
          }});
        </script>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Order agent</title>
  <style>
    :root {{ color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }}
    body {{ margin: 0; background: Canvas; color: CanvasText; }}
    main {{ width: min(48rem, calc(100% - 2rem)); height: calc(100vh - 2rem);
            margin: 1rem auto; display: flex; flex-direction: column; }}
    header, .composer {{ display: flex; gap: .75rem; align-items: center; }}
    header {{ justify-content: space-between; margin-bottom: 2rem; }}
    h1 {{ margin: 0; }}
    #messages {{ flex: 1; min-height: 0; overflow-y: auto; padding: .5rem; }}
    #messages p {{ max-width: 80%; padding: .75rem 1rem; border-radius: .75rem; }}
    #messages .user {{ margin-left: auto; background: Highlight; color: HighlightText; }}
    #messages .agent {{ background: color-mix(in srgb, CanvasText 10%, Canvas); }}
    #messages .error {{ color: #b42318; }}
    label {{ display: block; margin-bottom: .4rem; }}
    #chat-form {{ margin-top: 1rem; }}
    input {{ flex: 1; padding: .75rem; font: inherit; }}
    button, .button {{ padding: .7rem 1rem; font: inherit; cursor: pointer; }}
    .button {{ display: inline-block; background: Highlight; color: HighlightText;
               text-decoration: none; border-radius: .35rem; }}
    .secondary {{ background: transparent; border: 1px solid GrayText; }}
  </style>
</head>
<body>{body}</body>
</html>"""


def create_app(settings: Settings | None = None) -> Starlette:
    config = settings or Settings.from_env()
    sessions: dict[str, BrowserSession] = {}

    def current_session(request: Request) -> tuple[str, BrowserSession] | None:
        session_id = request.cookies.get(COOKIE_NAME)
        if not session_id:
            return None
        session = sessions.get(session_id)
        if session is None or session.expires_at <= time.time():
            sessions.pop(session_id, None)
            return None
        session.expires_at = time.time() + SESSION_TTL_SECONDS
        return session_id, session

    def require_csrf(request: Request, session: BrowserSession) -> bool:
        supplied = request.headers.get("X-CSRF-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, session.csrf_token)

    async def index(request: Request) -> HTMLResponse:
        active = current_session(request)
        if active is None:
            return HTMLResponse(_page(None))
        _, session = active
        return HTMLResponse(_page(session.profile, session.csrf_token))

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def login(_: Request) -> RedirectResponse:
        session_id = secrets.token_urlsafe(32)
        session = BrowserSession()
        sessions[session_id] = session
        app, _ = _msal_app(config, session)
        flow = await asyncio.to_thread(
            app.initiate_auth_code_flow,
            scopes=[config.mcp_scope],
            redirect_uri=config.redirect_uri,
            response_mode="form_post",
        )
        session.auth_flow = flow
        response = RedirectResponse(str(flow["auth_uri"]), status_code=302)
        _cookie(response, session_id)
        return response

    async def auth_callback(request: Request) -> Response:
        active = current_session(request)
        if active is None:
            return HTMLResponse("Authentication session expired.", status_code=400)
        session_id, session = active
        if session.auth_flow is None:
            return HTMLResponse("Authentication flow is missing.", status_code=400)

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
                return HTMLResponse("Authentication state is invalid.", status_code=400)
            finally:
                session.auth_flow = None

            if "error" in result:
                detail = html.escape(str(result.get("error_description", result["error"])))
                return HTMLResponse(f"Authentication failed: {detail}", status_code=400)

            session.profile = dict(result.get("id_token_claims") or {})
            _save_cache(session, cache)

        response = RedirectResponse("/", status_code=303)
        _cookie(response, session_id)
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

    async def chat(request: Request) -> JSONResponse:
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

        async with session.lock:
            token = await delegated_token(session)
            if token is None:
                return JSONResponse({"error": "Sign-in expired"}, status_code=401)

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
                    allowed_tools=ORDER_TOOLS,
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
                    instructions=(
                        "You help the signed-in user with their orders. "
                        "Use the order tools for order facts and never invent order data. "
                        "Only create an order or change status when the user explicitly asks."
                    ),
                    tools=[mcp],
                )
                async with agent:
                    if session.agent_session is None:
                        session.agent_session = agent.create_session(
                            session_id=session_id
                        )
                    result = await agent.run(
                        payload.message,
                        session=session.agent_session,
                    )

        return JSONResponse({"response": result.text})

    async def logout(request: Request) -> RedirectResponse | JSONResponse:
        active = current_session(request)
        if active is None:
            return RedirectResponse("/", status_code=303)
        session_id, session = active
        if not require_csrf(request, session):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
        sessions.pop(session_id, None)
        response = RedirectResponse(
            "https://login.microsoftonline.com/"
            f"{config.tenant_id}/oauth2/v2.0/logout?"
            + urlencode({"post_logout_redirect_uri": config.app_url}),
            status_code=303,
        )
        response.delete_cookie(COOKIE_NAME)
        return response

    return Starlette(
        debug=False,
        routes=[
            Route("/", index),
            Route("/health", health),
            Route("/login", login),
            Route("/auth/callback", auth_callback, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
        ],
    )


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        access_log=False,
    )
