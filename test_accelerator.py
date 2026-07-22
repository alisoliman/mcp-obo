from __future__ import annotations

import asyncio
import tempfile
import unittest
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from starlette.testclient import TestClient

import src.server as server
from src.chat import (
    ChatRequest,
    Settings,
    _safe_event_value,
    _sse_text_event,
    _workload_assertion,
    create_app,
)
from src.server import (
    GraphUser,
    OrderItem,
    OrderStatus,
    create_order_for,
    get_order_for,
    identity_proof_for,
    list_orders_for,
    update_order_status_for,
)


ROOT = Path(__file__).resolve().parent


class OrderTests(unittest.TestCase):
    def setUp(self) -> None:
        server._orders.clear()
        server._order_numbers = count(1)

    def test_create_calculates_total_and_lists_by_owner(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [
                OrderItem(sku="ABC-1", quantity=2, unit_price_cents=1250),
                OrderItem(sku="XYZ_2", quantity=1, unit_price_cents=499),
            ],
        )

        self.assertEqual(order.order_id, "ORD-000001")
        self.assertEqual(order.total_cents, 2999)
        self.assertEqual(list_orders_for("user-a"), [order])
        self.assertEqual(list_orders_for("user-b"), [])

    def test_owner_isolation_hides_existing_order(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        with self.assertRaisesRegex(ValueError, "Order not found"):
            get_order_for("user-b", order.order_id)

    def test_valid_status_sequence_and_idempotency(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        processing = update_order_status_for(
            "user-a", order.order_id, OrderStatus.PROCESSING
        )
        self.assertIs(
            update_order_status_for(
                "user-a", order.order_id, OrderStatus.PROCESSING
            ),
            processing,
        )
        update_order_status_for("user-a", order.order_id, OrderStatus.SHIPPED)
        delivered = update_order_status_for(
            "user-a", order.order_id, OrderStatus.DELIVERED
        )
        self.assertEqual(delivered.status, OrderStatus.DELIVERED)

    def test_invalid_status_transition_fails(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        with self.assertRaisesRegex(
            ValueError, "Cannot move an order from pending to shipped"
        ):
            update_order_status_for("user-a", order.order_id, OrderStatus.SHIPPED)

    def test_item_validation_rejects_invalid_values(self) -> None:
        invalid_items = (
            {"sku": "spaces are invalid", "quantity": 1, "unit_price_cents": 1},
            {"sku": "ABC", "quantity": 0, "unit_price_cents": 1},
            {"sku": "ABC", "quantity": 1, "unit_price_cents": -1},
        )
        for item in invalid_items:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                OrderItem.model_validate(item)

        with self.assertRaisesRegex(ValueError, "at least one item"):
            create_order_for("user-a", "Ada", [])

    def test_identity_proof_is_stable_without_exposing_graph_id(self) -> None:
        user = GraphUser(
            id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            displayName="Ada",
        )
        proof = identity_proof_for(user)
        self.assertEqual(proof.display_name, "Ada")
        self.assertEqual(proof, identity_proof_for(user))
        self.assertEqual(len(proof.identity_fingerprint), 8)
        self.assertNotIn(user.id, proof.model_dump_json())
        self.assertEqual(proof.evidence, "Microsoft Graph /me via OBO")


class ServerContractTests(unittest.TestCase):
    def test_server_requires_user_scope_and_exposes_only_order_tools(self) -> None:
        environment = {
            "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
            "ENTRA_SERVER_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
            "MCP_SERVER_URL": "https://orders.example.test",
        }
        with patch.dict("os.environ", environment, clear=False):
            mcp = server.create_server()
            tools = asyncio.run(mcp.list_tools())

        self.assertEqual(
            {tool.name for tool in tools},
            {
                "list_orders",
                "get_order",
                "create_order",
                "update_order_status",
                "who_am_i",
            },
        )
        long_text = "x" * 5_000
        self.assertIn(long_text, _sse_text_event("response.delta", long_text))
        self.assertEqual(mcp.auth.token_verifier.required_scopes, ["Mcp.Tools.ReadWrite"])
        for tool in tools:
            self.assertNotIn("access_token", tool.parameters.get("properties", {}))


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
    def acquire_token_by_auth_code_flow(
        self, _: object, __: object
    ) -> dict[str, str]:
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

    def test_workload_assertion_and_chat_input_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(" assertion \n", encoding="utf-8")
            self.assertEqual(_workload_assertion(self.settings(token_file)), "assertion")

        self.assertEqual(ChatRequest(message=" list orders ").message, " list orders ")
        with self.assertRaises(ValidationError):
            ChatRequest(message="")
        self.assertEqual(
            _safe_event_value(
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

    def test_live_client_flow_forwards_user_token_to_agent_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)
            with (
                patch("src.chat.ConfidentialClientApplication", FakeMsalApp),
                patch("src.chat.WorkloadIdentityCredential", FakeCredential),
                patch("src.chat.FoundryChatClient", lambda **_: object()),
                patch("src.chat.Agent", FakeAgent),
                TestClient(
                    create_app(settings, frontend_dist=frontend_dist),
                    base_url="https://orders.example.test",
                    follow_redirects=False,
                ) as client,
            ):
                login = client.get("/login")
                self.assertEqual(login.status_code, 302)
                self.assertEqual(login.headers["location"], "https://login.example/authorize")

                callback = client.post(
                    "/auth/callback",
                    data={"state": "state", "code": "code"},
                )
                self.assertEqual(callback.status_code, 303)

                page = client.get("/")
                self.assertIn('<div id="root"></div>', page.text)

                browser_session = client.get("/api/session")
                self.assertEqual(browser_session.status_code, 200)
                session_payload = browser_session.json()
                self.assertTrue(session_payload["authenticated"])
                self.assertEqual(session_payload["profile"]["name"], "Ada")
                self.assertTrue(session_payload["historyKey"])
                self.assertEqual(session_payload["model"], "gpt-5.6-terra")
                self.assertEqual(
                    session_payload["mcp"]["tools"],
                    [
                        "create_order",
                        "get_order",
                        "list_orders",
                        "update_order_status",
                        "who_am_i",
                    ],
                )

                response = client.post(
                    "/api/chat/stream",
                    headers={"X-CSRF-Token": session_payload["csrfToken"]},
                    json={"message": "What orders do I have?"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
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
        self.assertEqual(
            session.json(),
            {
                "authenticated": False,
                "model": "gpt-5.6-terra",
                "mcp": {
                    "name": "Order MCP",
                    "tools": [
                        "create_order",
                        "get_order",
                        "list_orders",
                        "update_order_status",
                        "who_am_i",
                    ],
                },
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(asset.text, "window.ORDER_AGENT = true")

    def test_auth_errors_and_logout_failures_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "token")
            frontend_dist = self.frontend_dist(directory)

            with (
                patch("src.chat.ConfidentialClientApplication", FakeMsalErrorApp),
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
                patch("src.chat.ConfidentialClientApplication", FakeMsalApp),
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


class DeploymentContractTests(unittest.TestCase):
    def test_bicep_declares_two_apps_foundry_and_native_consent(self) -> None:
        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("Microsoft.CognitiveServices/accounts/projects@2025-06-01", bicep)
        self.assertIn("var modelName = 'gpt-5.6-terra'", bicep)
        self.assertIn("Microsoft.Graph/oauth2PermissionGrants@v1.0", bicep)
        self.assertIn("'${appUrl}/auth/callback'", bicep)
        self.assertIn("isFallbackPublicClient: false", bicep)
        self.assertIn("redirectUris: []", bicep)

    def test_azd_uses_two_aks_services_without_python_deployer(self) -> None:
        azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")
        self.assertIn("  mcp:\n", azure_yaml)
        self.assertIn("  chat:\n", azure_yaml)
        self.assertIn("host: aks", azure_yaml)
        self.assertIn("helm upgrade --install", azure_yaml)
        self.assertFalse((ROOT / "scripts" / "deploy.py").exists())

    def test_chat_image_builds_and_serves_the_shadcn_frontend(self) -> None:
        dockerfile = (ROOT / "Dockerfile.chat").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("FROM node:22-alpine AS frontend", dockerfile)
        self.assertIn("COPY --from=frontend /frontend/dist ./frontend/dist", dockerfile)
        self.assertIn("MessageScrollerProvider", app)
        self.assertIn("BubbleContent", app)
        self.assertIn('fetch("/api/session"', app)
        self.assertIn('fetch("/api/chat/stream"', app)
        self.assertIn("<ToolHeader", app)
        self.assertIn("historyStorageKey(session.historyKey)", app)
        self.assertIn('role="log"', app)

    def test_ingress_routes_chat_and_mcp_separately(self) -> None:
        ingress = (ROOT / "k8s" / "chat" / "ingress.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("path: /mcp", ingress)
        self.assertIn("name: fastmcp-orders", ingress)
        self.assertIn("path: /", ingress)
        self.assertIn("name: order-agent-chat", ingress)


if __name__ == "__main__":
    unittest.main()
