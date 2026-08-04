"""The tool names, checked against what a registered server actually offers.

`src/shared/tools.py` is imported by both workloads: the server registers these
and the chat app passes them to the agent as its allow-list. A name that drifts
between the two does not fail loudly -- the tool simply stops being callable and
the agent reports that it cannot do the thing -- so the agreement is asserted
here rather than left to a comment.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import src.mcp_server.server as server
from src.chat_app.prompts import allowed_tools
from src.shared.tools import DATABRICKS_TOOLS, ORDER_TOOLS
from tests.helpers import DATABRICKS_ENVIRONMENT, MCP_ENVIRONMENT


def registered_tools(environment: dict[str, str]) -> set[str]:
    with patch.dict("os.environ", environment, clear=True):
        return {tool.name for tool in asyncio.run(server.create_server().list_tools())}


class ToolNamesAreOneContract(unittest.TestCase):
    def test_orders_mode_registers_exactly_the_declared_names(self) -> None:
        self.assertEqual(registered_tools(MCP_ENVIRONMENT), set(ORDER_TOOLS))

    def test_databricks_mode_registers_exactly_the_declared_names(self) -> None:
        environment = dict(
            MCP_ENVIRONMENT, MCP_MODE="databricks", **DATABRICKS_ENVIRONMENT
        )
        self.assertEqual(registered_tools(environment), set(DATABRICKS_TOOLS))

    def test_the_agent_is_given_the_names_the_server_registered(self) -> None:
        """The allow-list the chat app hands the agent, for each mode."""
        with patch.dict("os.environ", MCP_ENVIRONMENT, clear=True):
            self.assertEqual(allowed_tools(), ORDER_TOOLS)
        environment = dict(
            MCP_ENVIRONMENT, MCP_MODE="databricks", **DATABRICKS_ENVIRONMENT
        )
        with patch.dict("os.environ", environment, clear=True):
            self.assertEqual(allowed_tools(), DATABRICKS_TOOLS)
