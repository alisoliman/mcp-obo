"""The gates every request passes before a tool body runs."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import src.mcp_server.server as server
from src.shared.tools import ORDER_TOOLS
from tests.helpers import (
    MCP_ENVIRONMENT,
)


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
        self.assertEqual(set(verifier.audience), {client_id, f"api://{client_id}"})
        self.assertEqual(
            verifier.issuer,
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        )

    def test_annotations_tell_a_client_which_tools_change_something(self) -> None:
        # Annotations are hints and this server still decides for itself, but
        # they are the only thing a client can read to apply a policy this
        # server cannot apply for it -- an approval prompt, a read-only session.
        # Unannotated means writing and destructive by MCP's own default, so the
        # two order tools that really do write have to say so themselves.
        environment = dict(MCP_ENVIRONMENT, DATABRICKS_HOST="")
        with patch.dict("os.environ", environment, clear=False):
            tools = asyncio.run(server.create_server().list_tools())

        read_only = {
            tool.name for tool in tools if tool.annotations.readOnlyHint is True
        }
        self.assertEqual(read_only, {"who_am_i", "list_orders", "get_order"})
        writing = {tool.name: tool.annotations for tool in tools}
        self.assertFalse(writing["create_order"].readOnlyHint)
        self.assertFalse(writing["update_order_status"].readOnlyHint)
        # Cancellation is one of the exits from the status machine, so a client
        # is right to treat a status change as destructive and a creation as not.
        self.assertFalse(writing["create_order"].destructiveHint)
        self.assertTrue(writing["update_order_status"].destructiveHint)

    def test_a_cross_origin_caller_is_refused_before_the_auth_layer(self) -> None:
        # The middleware is wired in `__main__` rather than by `create_server`,
        # so the app is assembled here the same way the server assembles it.
        environment = dict(MCP_ENVIRONMENT, DATABRICKS_HOST="")
        with patch.dict("os.environ", environment, clear=False):
            mcp = server.create_server()
            app = mcp.http_app(
                path=server.MCP_PATH,
                middleware=[
                    Middleware(server.SameOriginOnly, allowed=server.allowed_origins())
                ],
            )

        call = {
            "json": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            "headers": {"Accept": "application/json, text/event-stream"},
        }
        with TestClient(app, base_url=MCP_ENVIRONMENT["MCP_SERVER_URL"]) as client:
            hostile = client.post(
                server.MCP_PATH,
                headers={**call["headers"], "Origin": "https://attacker.example"},
                json=call["json"],
            )
            same = client.post(
                server.MCP_PATH,
                headers={
                    **call["headers"],
                    "Origin": MCP_ENVIRONMENT["MCP_SERVER_URL"],
                },
                json=call["json"],
            )
            absent = client.post(server.MCP_PATH, **call)
            discovery = client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Origin": "https://attacker.example"},
            )

        self.assertEqual(hostile.status_code, 403)
        # 401 rather than 200: the origin check is a gate in front of the token
        # check, never a substitute for it. Only a browser sends Origin, and the
        # chat app calls this endpoint from its own process without one, so an
        # absent header has to fall through to the same place a same-origin
        # request does.
        self.assertEqual(same.status_code, 401)
        self.assertEqual(absent.status_code, 401)
        # A client reads the discovery document before it has a token, from
        # wherever it happens to run, so this one is exempt on purpose.
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(
            discovery.json()["resource"],
            f"{MCP_ENVIRONMENT['MCP_SERVER_URL']}{server.MCP_PATH}",
        )
