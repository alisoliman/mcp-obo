"""The browser-facing half: sessions, cookies, CSRF and the event stream."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from starlette.testclient import TestClient

import src.mcp_server.server as server
from src.chat_app.app import ChatRequest, create_app
from src.chat_app.auth import workload_assertion
from src.chat_app.events import safe_event_value, sse_text_event, tool_result_value
from src.chat_app.prompts import allowed_tools
from src.chat_app.sessions import (
    COOKIE_NAME,
    PRE_AUTH_TTL_SECONDS,
    SESSION_ABSOLUTE_TTL_SECONDS,
    SESSION_IDLE_TTL_SECONDS,
    BrowserSession,
)
from src.chat_app.settings import Settings
from tests.helpers import (
    csp_directives,
    session_cookie,
)


class FakeMsalApp:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def initiate_auth_code_flow(self, **_: object) -> dict[str, str]:
        return {"auth_uri": "https://login.example/authorize", "state": "state"}

    def acquire_token_by_auth_code_flow(
        self, _: object, __: object
    ) -> dict[str, object]:
        return {
            "access_token": "mcp-user-token",
            "id_token_claims": {"name": "Ada"},
        }

    def get_accounts(self) -> list[dict[str, str]]:
        return [{"home_account_id": "ada"}]

    def acquire_token_silent(self, *_: object, **__: object) -> dict[str, str]:
        return {"access_token": "mcp-user-token"}


class FakeMsalErrorApp(FakeMsalApp):
    def acquire_token_by_auth_code_flow(self, _: object, __: object) -> dict[str, str]:
        return {
            "error": "access_denied",
            "error_description": "The user denied consent.",
        }


class FakeCredential:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    async def __aenter__(self) -> FakeCredential:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class FakeAgent:
    authorization = ""
    message = ""

    def __init__(self, *_: object, tools: list[object], **__: object) -> None:
        self.tools = tools

    async def __aenter__(self) -> FakeAgent:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def create_session(self, **_: object) -> object:
        return object()

    def run(
        self,
        message: str,
        *,
        stream: bool = False,
        middleware: list[object] | None = None,
        **_: object,
    ) -> FakeResponseStream | SimpleNamespace:
        FakeAgent.authorization = str(
            self.tools[0]._httpx_client.headers["Authorization"]
        )
        FakeAgent.message = message
        if stream:
            return FakeResponseStream(middleware or [])
        return SimpleNamespace(text="You have one pending order.")


class FakeResponseStream:
    def __init__(self, middleware: list[object]) -> None:
        self.middleware = middleware
        self.invoked = False
        self._updates = iter(
            [
                SimpleNamespace(
                    contents=[
                        SimpleNamespace(
                            type="text",
                            text="You have one pending order.",
                        )
                    ]
                ),
            ]
        )

    def __aiter__(self) -> FakeResponseStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if not self.invoked:
            self.invoked = True
            context = SimpleNamespace(
                function=SimpleNamespace(name="who_am_i"),
                arguments={},
                result=None,
            )

            async def call_next() -> None:
                context.result = {
                    "display_name": "Ada",
                    "identity_fingerprint": "ABC12345",
                    "evidence": "Microsoft Graph /me via OBO",
                }

            await self.middleware[0].process(context, call_next)
        try:
            return next(self._updates)
        except StopIteration:
            raise StopAsyncIteration from None

    async def get_final_response(self) -> SimpleNamespace:
        return SimpleNamespace(text="You have one pending order.")


class ChatTests(unittest.TestCase):
    def settings(self, token_file: Path) -> Settings:
        return Settings(
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
            redirect_uri="https://orders.example.test/auth/callback",
            app_url="https://orders.example.test",
            mcp_scope="api://server/Mcp.Tools.ReadWrite",
            mcp_endpoint="https://orders.example.test/mcp",
            foundry_project_endpoint="https://foundry.example.test",
            foundry_model="gpt-5.6-terra",
            token_file=token_file,
        )

    def frontend_dist(self, directory: str) -> Path:
        root = Path(directory) / "frontend"
        assets = root / "assets"
        assets.mkdir(parents=True)
        (root / "index.html").write_text(
            '<!doctype html><div id="root"></div><script src="/assets/app.js"></script>',
            encoding="utf-8",
        )
        (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
        (assets / "app.js").write_text("window.ORDER_AGENT = true", encoding="utf-8")
        return root

    def test_stream_events_are_sanitized_before_reaching_the_browser(self) -> None:
        self.assertEqual(
            safe_event_value(
                {
                    "access_token": "secret",
                    "nested": {"Authorization": "Bearer secret"},
                    "message": "safe",
                }
            ),
            {
                "access_token": "[redacted]",
                "nested": {"Authorization": "[redacted]"},
                "message": "safe",
            },
        )
        # Agent text is never truncated on its way to the transcript, unlike the
        # tool payloads that `_safe_event_value` caps.
        long_text = "x" * 5_000
        self.assertIn(long_text, sse_text_event("response.delta", long_text))
        # Identical text blocks collapse to a single parsed result.
        duplicate_result = [
            SimpleNamespace(type="text", text='{"display_name":"Ada"}'),
            SimpleNamespace(type="text", text='{"display_name":"Ada"}'),
        ]
        self.assertEqual(tool_result_value(duplicate_result), {"display_name": "Ada"})

    def test_workload_assertion_and_chat_input_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(" assertion \n", encoding="utf-8")
            self.assertEqual(workload_assertion(self.settings(token_file)), "assertion")

        self.assertEqual(ChatRequest(message=" list orders ").message, " list orders ")
        with self.assertRaises(ValidationError):
            ChatRequest(message="")

    def test_live_client_flow_forwards_user_token_to_agent_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with (
                patch("src.chat_app.auth.ConfidentialClientApplication", FakeMsalApp),
                patch("src.chat_app.app.WorkloadIdentityCredential", FakeCredential),
                patch("src.chat_app.app.FoundryChatClient", lambda **_: object()),
                patch("src.chat_app.app.Agent", FakeAgent),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                login = client.get("/login")
                self.assertEqual(login.status_code, 302)
                self.assertEqual(
                    login.headers["location"], "https://login.example/authorize"
                )

                callback = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                )
                self.assertEqual(callback.status_code, 303)

                page = client.get("/")
                self.assertIn('<div id="root"></div>', page.text)
                self.assertEqual(page.headers["cache-control"], "no-cache")

                browser_session = client.get("/api/session")
                self.assertEqual(browser_session.status_code, 200)
                self.assertEqual(
                    browser_session.headers["cache-control"],
                    "no-store",
                )
                session_payload = browser_session.json()
                self.assertTrue(session_payload["authenticated"])
                self.assertEqual(session_payload["profile"]["name"], "Ada")
                self.assertEqual(session_payload["model"], "gpt-5.6-terra")
                self.assertEqual(session_payload["mode"], server.current_mode())
                # The order tools are named explicitly as well, so the Graph
                # half of the demo cannot quietly disappear from the UI.
                self.assertEqual(
                    session_payload["mcp"]["tools"],
                    sorted(allowed_tools()),
                )
                self.assertLessEqual(
                    {
                        "create_order",
                        "get_order",
                        "list_orders",
                        "update_order_status",
                        "who_am_i",
                    },
                    set(session_payload["mcp"]["tools"]),
                )

                response = client.post(
                    "/api/chat/stream",
                    headers={"X-CSRF-Token": session_payload["csrfToken"]},
                    json={"message": "What orders do I have?"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        # The stream is stamped by the same middleware as every other route.
        # It rewrites `http.response.start` and never touches the body, which is
        # what keeps the agent's token deltas from being queued behind it.
        self.assertEqual(csp_directives(response)["frame-ancestors"], ["'none'"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("event: response.started", response.text)
        self.assertIn("event: tool.started", response.text)
        self.assertIn('"name":"who_am_i"', response.text)
        self.assertIn("event: tool.completed", response.text)
        self.assertIn("ABC12345", response.text)
        self.assertIn("event: response.delta", response.text)
        self.assertIn("event: response.completed", response.text)
        self.assertEqual(FakeAgent.authorization, "Bearer mcp-user-token")
        self.assertEqual(FakeAgent.message, "What orders do I have?")

    def test_unauthenticated_and_csrf_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frontend_dist = self.frontend_dist(directory)
            with TestClient(
                create_app(
                    self.settings(Path(directory) / "token"),
                    frontend_dist=frontend_dist,
                ),
                base_url="https://orders.example.test",
            ) as client:
                session = client.get("/api/session")
                response = client.post("/api/chat/stream", json={"message": "hello"})
                asset = client.get("/assets/app.js")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.headers["cache-control"], "no-store")
        self.assertEqual(
            session.json(),
            {
                "authenticated": False,
                "model": "gpt-5.6-terra",
                "mode": "orders",
                "mcp": {"tools": sorted(allowed_tools())},
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(asset.text, "window.ORDER_AGENT = true")

    def test_auth_errors_and_logout_failures_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)

            with (
                patch(
                    "src.chat_app.auth.ConfidentialClientApplication", FakeMsalErrorApp
                ),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                client.get("/login")
                denied = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                )

            self.assertEqual(denied.status_code, 400)
            self.assertEqual(
                denied.text,
                "Authentication failed: The user denied consent.",
            )

            with (
                patch("src.chat_app.auth.ConfidentialClientApplication", FakeMsalApp),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                client.get("/login")
                client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                )
                browser_session = client.get("/api/session").json()

                rejected = client.post(
                    "/logout",
                    headers={"X-CSRF-Token": "wrong"},
                )
                still_authenticated = client.get("/api/session")
                accepted = client.post(
                    "/logout",
                    headers={"X-CSRF-Token": browser_session["csrfToken"]},
                )
                signed_out = client.get("/api/session")

            self.assertEqual(rejected.status_code, 403)
            self.assertTrue(still_authenticated.json()["authenticated"])
            self.assertEqual(accepted.status_code, 200)
            self.assertIn("/oauth2/v2.0/logout?", accepted.json()["logoutUrl"])
            self.assertFalse(signed_out.json()["authenticated"])

    def test_missing_frontend_build_fails_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "Frontend build is missing"):
                create_app(
                    self.settings(Path(directory) / "token"),
                    frontend_dist=Path(directory) / "missing",
                )

    def test_signing_in_rotates_the_session_and_kills_the_old_one(self) -> None:
        created: list[BrowserSession] = []

        class RecordedSession(BrowserSession):
            """Records each session so the pre-authentication CSRF token can be
            read; nothing exposes it over HTTP before sign-in."""

            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with (
                patch("src.chat_app.auth.ConfidentialClientApplication", FakeMsalApp),
                patch("src.chat_app.app.BrowserSession", RecordedSession),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                planted = session_cookie(client.get("/login")).value
                pre_auth_csrf = created[0].csrf_token

                # TestClient keeps its own cookie jar, which would follow the
                # rotation and hide the whole point. Every request below is
                # driven with an explicit cookie so both ids can be presented.
                client.cookies.clear()
                callback = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                    headers={"Cookie": f"{COOKIE_NAME}={planted}"},
                )
                rotated = session_cookie(callback).value
                client.cookies.clear()

                replayed = client.get(
                    "/api/session", headers={"Cookie": f"{COOKIE_NAME}={planted}"}
                )
                client.cookies.clear()
                signed_in = client.get(
                    "/api/session", headers={"Cookie": f"{COOKIE_NAME}={rotated}"}
                )
                client.cookies.clear()
                stale_csrf = client.post(
                    "/logout",
                    headers={
                        "Cookie": f"{COOKIE_NAME}={rotated}",
                        "X-CSRF-Token": pre_auth_csrf,
                    },
                )

        self.assertEqual(callback.status_code, 303)
        self.assertNotEqual(rotated, planted)
        # Rotation on its own is not the fix. The id that arrived has to stop
        # resolving, or an attacker who planted it -- `cloudapp.azure.com` is
        # not on the Public Suffix List, so a sibling label can set a cookie
        # here -- holds a copy that follows the user in. Everything downstream
        # would then correctly believe the attacker is this user, because this
        # is the only layer that decides who asked.
        self.assertFalse(replayed.json()["authenticated"])
        self.assertTrue(signed_in.json()["authenticated"])
        # The CSRF token is the second half of the same trust and is minted
        # before authentication too, so it rotates with the id.
        self.assertNotEqual(signed_in.json()["csrfToken"], pre_auth_csrf)
        self.assertEqual(stale_csrf.status_code, 403)

    def test_the_session_cookie_meets_every_host_prefix_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with (
                patch("src.chat_app.auth.ConfidentialClientApplication", FakeMsalApp),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                login = client.get("/login")
                callback = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                )
                csrf_token = client.get("/api/session").json()["csrfToken"]
                signed_out = client.post(
                    "/logout", headers={"X-CSRF-Token": csrf_token}
                )

        for stage, response in (
            ("login", login),
            ("callback", callback),
            ("logout", signed_out),
        ):
            with self.subTest(stage=stage):
                cookie = session_cookie(response)
                # A browser drops a `__Host-` cookie outright unless every one
                # of these holds, so asserting the name alone would assert
                # nothing at all. The deletion cookie is where this usually goes
                # wrong: a plain delete_cookie omits Secure, the browser refuses
                # it, and the user stays signed in after clicking sign out.
                self.assertEqual(cookie.key, "__Host-order_agent_session")
                self.assertTrue(cookie["secure"])
                self.assertTrue(cookie["httponly"])
                self.assertEqual(cookie["path"], "/")
                self.assertEqual(cookie["domain"], "")
                # Entra returns the authorization code as a cross-site form
                # POST, which a SameSite=Lax cookie would not accompany. The
                # CSRF header check is what stands in for SameSite here.
                self.assertEqual(cookie["samesite"], "none")

        # Ten minutes before sign-in, because that is how long Entra keeps the
        # authorization code alive: expiring first would fail a sign-in that is
        # still valid, which is what a user pausing on an MFA prompt looks like.
        self.assertEqual(session_cookie(login)["max-age"], str(PRE_AUTH_TTL_SECONDS))
        # The cookie carries the absolute lifetime. The idle timeout is the
        # server's business and is not something the browser should enforce.
        self.assertEqual(
            session_cookie(callback)["max-age"], str(SESSION_ABSOLUTE_TTL_SECONDS)
        )
        self.assertEqual(session_cookie(signed_out)["max-age"], "0")
        self.assertEqual(session_cookie(signed_out).value, "")

    def test_a_session_slides_only_once_authenticated_and_stops_at_the_ceiling(
        self,
    ) -> None:
        session = BrowserSession()
        pre_auth_deadline = session.expires_at
        self.assertAlmostEqual(
            pre_auth_deadline - time.time(), PRE_AUTH_TTL_SECONDS, delta=5
        )
        # A pre-authentication session is the one an unauthenticated caller can
        # mint. Sliding it would hand back the unbounded lifetime that polling
        # /api/session in a loop is otherwise good for.
        session.touch(time.time() + 60)
        self.assertEqual(session.expires_at, pre_auth_deadline)

        signed_in_at = 1_700_000_000.0
        session.authenticate(signed_in_at)
        self.assertEqual(session.expires_at, signed_in_at + SESSION_IDLE_TTL_SECONDS)
        session.touch(signed_in_at + 600)
        self.assertEqual(
            session.expires_at, signed_in_at + 600 + SESSION_IDLE_TTL_SECONDS
        )

        # The clamp is the point of the ceiling: without it a stolen cookie
        # stays usable for as long as MSAL can keep refreshing, which outlives
        # whatever sign-in frequency the tenant's Conditional Access policy
        # asked for.
        ceiling = signed_in_at + SESSION_ABSOLUTE_TTL_SECONDS
        session.touch(ceiling - 60)
        self.assertEqual(session.expires_at, ceiling)
        session.touch(ceiling + 3_600)
        self.assertEqual(session.expires_at, ceiling)

    def test_the_session_store_is_capped_against_a_login_flood(self) -> None:
        # The store is per-pod and in memory, so an unauthenticated GET /login
        # loop is an allocation primitive. The cap is lowered here rather than
        # minting two thousand sessions to watch it hold.
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with (
                patch("src.chat_app.auth.ConfidentialClientApplication", FakeMsalApp),
                patch("src.chat_app.sessions.MAX_SESSIONS", 3),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                flood = []
                for _ in range(4):
                    flood.append(session_cookie(client.get("/login")).value)
                    client.cookies.clear()

                evicted = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                    headers={"Cookie": f"{COOKIE_NAME}={flood[0]}"},
                )
                client.cookies.clear()
                survivor = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                    headers={"Cookie": f"{COOKIE_NAME}={flood[-1]}"},
                )

        # The soonest-to-expire goes first, which under a flood means the
        # pre-authentication shells the flood itself created.
        self.assertEqual(evicted.status_code, 400)
        self.assertEqual(evicted.text, "Authentication session expired.")
        self.assertEqual(survivor.status_code, 303)

    def test_security_headers_are_stamped_on_every_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with TestClient(
                create_app(settings, frontend_dist=frontend_dist),
                base_url="https://orders.example.test",
            ) as client:
                responses = {
                    "index": client.get("/"),
                    "api": client.get("/api/session"),
                    "asset": client.get("/assets/app.js"),
                }

        for name, response in responses.items():
            with self.subTest(route=name):
                directives = csp_directives(response)
                # The session cookie has to stay SameSite=none for Entra's
                # cross-site form POST, so it rides along inside a hostile
                # iframe. Without frame-ancestors an attacker page can frame the
                # signed-in UI and drive a real turn by redressing the button.
                self.assertEqual(directives["frame-ancestors"], ["'none'"])
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                # img-src is the prompt-injection exfiltration channel: table
                # COMMENT text from Unity Catalog reaches the model, and a
                # remote <img> in its markdown would be a silent GET carrying
                # whatever it was told to encode. A wildcard here reopens it.
                self.assertNotIn("*", " ".join(directives["img-src"]))
                self.assertEqual(directives["default-src"], ["'self'"])
                self.assertEqual(directives["object-src"], ["'none'"])
                self.assertEqual(directives["base-uri"], ["'none'"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertIn(
                    "max-age=31536000", response.headers["strict-transport-security"]
                )
