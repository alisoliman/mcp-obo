from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from pydantic import ValidationError
from starlette.testclient import TestClient

import src.orders as orders
import src.server as server
from src import databricks
from src.databricks import DATABRICKS_TOOLS
from src.orders import ORDER_TOOLS
from src.chat import (
    allowed_tools,
    ChatRequest,
    Settings,
    _safe_event_value,
    _sse_text_event,
    _tool_result_value,
    _workload_assertion,
    create_app,
)
from src.obo import DATABRICKS_RESOURCE_APP_ID, DATABRICKS_SCOPE, GRAPH_SCOPE
from src.orders import (
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

MCP_ENVIRONMENT = {
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "ENTRA_SERVER_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "MCP_SERVER_URL": "https://orders.example.test",
    "MCP_MODE": "orders",
}
DATABRICKS_ENVIRONMENT = {
    "DATABRICKS_HOST": "https://adb-1234567890.11.azuredatabricks.net",
    "DATABRICKS_WAREHOUSE_ID": "0123456789abcdef",
    "DATABRICKS_CATALOG": "mcp_obo_demo",
    "DATABRICKS_SCHEMA": "sales",
}


class OrderTests(unittest.TestCase):
    def setUp(self) -> None:
        orders._orders.clear()
        orders._order_numbers = count(1)

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


class McpServerTests(unittest.TestCase):
    def test_server_requires_user_scope_and_exposes_only_order_tools(self) -> None:
        # Cleared explicitly: the Databricks tools register themselves off
        # DATABRICKS_HOST, so a developer with a workspace in their shell would
        # see this fail for the wrong reason.
        environment = dict(MCP_ENVIRONMENT, DATABRICKS_HOST="")
        with patch.dict("os.environ", environment, clear=False):
            mcp = server.create_server()
            tools = asyncio.run(mcp.list_tools())

        self.assertEqual({tool.name for tool in tools}, ORDER_TOOLS)
        self.assertEqual(
            mcp.auth.token_verifier.required_scopes, ["Mcp.Tools.ReadWrite"]
        )
        # The caller's token is injected by FastMCP, never accepted as an
        # argument, so a model cannot talk the server into a different identity.
        for tool in tools:
            self.assertNotIn("access_token", tool.parameters.get("properties", {}))

    def test_token_audience_is_pinned_to_this_server(self) -> None:
        client_id = MCP_ENVIRONMENT["ENTRA_SERVER_CLIENT_ID"]
        tenant_id = MCP_ENVIRONMENT["AZURE_TENANT_ID"]
        verifier = AzureJWTVerifier(
            client_id=client_id,
            tenant_id=tenant_id,
            required_scopes=["Mcp.Tools.ReadWrite"],
        )
        # A token minted for any other resource fails audience validation, even
        # if it carries a scope with the same name.
        self.assertEqual(
            set(verifier.audience), {client_id, f"api://{client_id}"}
        )
        self.assertEqual(
            verifier.issuer,
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        )


class DatabricksToolTests(unittest.TestCase):
    """Databricks mode, checked without a workspace.

    What a unit test can prove here is narrow: the mode selects the right tools,
    the query is not assembled from caller input, and the Entra permission is
    delegated rather than application. Whether Unity Catalog actually filters
    rows is a deployment fact, checked by `test_databricks_e2e.py`.
    """

    def test_databricks_mode_replaces_the_order_tools(self) -> None:
        environment = dict(
            MCP_ENVIRONMENT, MCP_MODE="databricks", **DATABRICKS_ENVIRONMENT
        )
        with patch.dict("os.environ", environment, clear=False):
            mcp = server.create_server()
            tools = asyncio.run(mcp.list_tools())
            allowed = allowed_tools()

        # The modes are alternatives, not layers.
        self.assertEqual({tool.name for tool in tools}, set(DATABRICKS_TOOLS))
        self.assertEqual({tool.name for tool in tools} & set(ORDER_TOOLS), set())
        self.assertEqual(allowed, DATABRICKS_TOOLS)
        for tool in tools:
            self.assertNotIn("access_token", tool.parameters.get("properties", {}))

    def test_orders_mode_serves_only_the_order_tools(self) -> None:
        # Databricks settings present but the mode says orders: the workspace
        # config is ignored rather than quietly bolted on.
        environment = dict(
            MCP_ENVIRONMENT, MCP_MODE="orders", **DATABRICKS_ENVIRONMENT
        )
        with patch.dict("os.environ", environment, clear=False):
            mcp = server.create_server()
            tools = asyncio.run(mcp.list_tools())
            allowed = allowed_tools()

        self.assertEqual({tool.name for tool in tools}, set(ORDER_TOOLS))
        self.assertEqual(allowed, ORDER_TOOLS)

    def test_an_unknown_mode_fails_at_startup(self) -> None:
        environment = dict(MCP_ENVIRONMENT, MCP_MODE="postgres")
        with patch.dict("os.environ", environment, clear=False), self.assertRaisesRegex(
            RuntimeError, "MCP_MODE must be one of"
        ):
            server.create_server()

    def test_databricks_mode_without_a_workspace_fails_at_startup(self) -> None:
        environment = dict(
            MCP_ENVIRONMENT, MCP_MODE="databricks", DATABRICKS_HOST=""
        )
        with patch.dict("os.environ", environment, clear=False), self.assertRaisesRegex(
            RuntimeError, "requires DATABRICKS_HOST"
        ):
            server.create_server()

    def test_partial_databricks_configuration_fails_at_startup(self) -> None:
        # Half-configured is worse than absent: the tools would appear in the
        # tool list and fail on first call, so startup refuses instead.
        environment = dict(
            MCP_ENVIRONMENT,
            MCP_MODE="databricks",
            DATABRICKS_HOST=DATABRICKS_ENVIRONMENT["DATABRICKS_HOST"],
            DATABRICKS_WAREHOUSE_ID="",
            DATABRICKS_CATALOG="",
            DATABRICKS_SCHEMA="",
        )
        with patch.dict("os.environ", environment, clear=False), self.assertRaisesRegex(
            RuntimeError, "DATABRICKS_WAREHOUSE_ID"
        ):
            server.create_server()

    def test_query_binds_parameters_instead_of_building_sql(self) -> None:
        statement = databricks.orders_statement("cat.sales.orders")
        injection = "EU' OR '1'='1"
        parameters = databricks.orders_parameters(injection, 25)

        # The attacker-controlled string reaches Databricks as a bound value,
        # never as statement text.
        self.assertNotIn(injection, statement)
        self.assertIn(":region", statement)
        self.assertIn(":row_limit", statement)
        self.assertEqual(
            parameters,
            [
                {"name": "region", "type": "STRING", "value": injection},
                {"name": "row_limit", "type": "INT", "value": "25"},
            ],
        )

        # A region of None must send no `value` at all. Sending an empty string
        # would make `:region IS NULL` false and silently return zero rows.
        self.assertEqual(
            databricks.orders_parameters(None, 5)[0],
            {"name": "region", "type": "STRING"},
        )

    def test_catalog_identifiers_are_rejected_when_not_plain(self) -> None:
        environment = dict(
            MCP_ENVIRONMENT,
            MCP_MODE="databricks",
            **{**DATABRICKS_ENVIRONMENT, "DATABRICKS_CATALOG": "cat; DROP TABLE x"},
        )
        with patch.dict("os.environ", environment, clear=False), self.assertRaisesRegex(
            RuntimeError, "valid Unity Catalog"
        ):
            server.create_server()

    def test_the_two_downstream_scopes_differ_only_by_resource(self) -> None:
        self.assertEqual(GRAPH_SCOPE, "https://graph.microsoft.com/.default")
        self.assertEqual(DATABRICKS_SCOPE, f"{DATABRICKS_RESOURCE_APP_ID}/.default")
        # A fixed Azure constant, identical in every tenant.
        self.assertEqual(
            DATABRICKS_RESOURCE_APP_ID, "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
        )

    def test_no_application_permission_is_ever_requested(self) -> None:
        # `type: 'Role'` is how a Bicep resourceAccess entry asks for an
        # application permission -- one that works with no user present -- and
        # the whole accelerator rests on there being none. Asserting against the
        # compiled ARM JSON means a rename cannot quietly turn this green.
        compiled = subprocess.run(
            ["az", "bicep", "build", "--file", str(ROOT / "infra" / "main.bicep"),
             "--stdout"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if compiled.returncode != 0:
            self.skipTest("az bicep is unavailable")

        template = json.loads(compiled.stdout)
        resources = template["resources"]
        entries = resources.values() if isinstance(resources, dict) else resources

        requested = [
            access
            for resource in entries
            if str(resource.get("type", "")).startswith("Microsoft.Graph/applications")
            for required in (
                resource.get("properties", {}).get("requiredResourceAccess") or []
            )
            # Some entries are ARM expression strings that copy another
            # application's list verbatim; following the reference would only
            # double-count what is already checked where it is declared.
            if isinstance(required, dict)
            for access in (required.get("resourceAccess") or [])
        ]
        self.assertTrue(requested, "expected the delegated permissions to be present")
        for access in requested:
            self.assertEqual(
                access.get("type"), "Scope", f"application permission found: {access}"
            )

        # Permission ids compile to variable references, so the delegated
        # Databricks permission is checked where the compiler resolved it.
        self.assertEqual(
            template["variables"]["databricksUserImpersonationScopeId"],
            "739272be-e143-11e8-9f32-f2801f1b9fd1",
        )
        self.assertEqual(
            template["variables"]["databricksAppId"],
            "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d",
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

    def test_stream_events_are_sanitized_before_reaching_the_browser(self) -> None:
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
        # Agent text is never truncated on its way to the transcript, unlike the
        # tool payloads that `_safe_event_value` caps.
        long_text = "x" * 5_000
        self.assertIn(long_text, _sse_text_event("response.delta", long_text))
        # Identical text blocks collapse to a single parsed result.
        duplicate_result = [
            SimpleNamespace(type="text", text='{"display_name":"Ada"}'),
            SimpleNamespace(type="text", text='{"display_name":"Ada"}'),
        ]
        self.assertEqual(
            _tool_result_value(duplicate_result), {"display_name": "Ada"}
        )

    def test_workload_assertion_and_chat_input_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(" assertion \n", encoding="utf-8")
            self.assertEqual(_workload_assertion(self.settings(token_file)), "assertion")

        self.assertEqual(ChatRequest(message=" list orders ").message, " list orders ")
        with self.assertRaises(ValidationError):
            ChatRequest(message="")

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


if __name__ == "__main__":
    unittest.main()
