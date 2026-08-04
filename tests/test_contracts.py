"""The names one mode's workloads have to agree on, checked rather than trusted.

`src/shared/tools.py` is imported by both workloads: the server registers these
and the chat app passes them to the agent as its allow-list. A name that drifts
between the two does not fail loudly -- the tool simply stops being callable and
the agent reports that it cannot do the thing -- so the agreement is asserted
here rather than left to a comment.

The mode names are the same kind of agreement, spread wider: a mode names the
file its instructions are read from, the tool set the chat app hands the agent,
and a key the browser looks up. The template's half of that agreement needs a
Bicep compile, so it lives in tests/test_infrastructure.py instead.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from unittest.mock import patch

import src.mcp_server.server as server
from src.chat_app.prompts import _read, agent_instructions, allowed_tools
from src.shared.modes import MODES
from src.shared.tools import DATABRICKS_TOOLS, ORDER_TOOLS
from tests.helpers import DATABRICKS_ENVIRONMENT, MCP_ENVIRONMENT, ROOT

# The `modes` object in App.tsx, and its top-level keys. Read with a regex
# because the alternative is a JavaScript parser in a Python suite.
FRONTEND_MODES = re.compile(
    r"^const modes = \{$(.*?)^\} as const$", re.MULTILINE | re.DOTALL
)
FRONTEND_MODE_KEY = re.compile(r"^  (\w+): \{$", re.MULTILINE)


def registered_tools(environment: dict[str, str]) -> set[str]:
    with patch.dict("os.environ", environment, clear=True):
        return {tool.name for tool in asyncio.run(server.create_server().list_tools())}


class ToolNamesAreOneContract(unittest.TestCase):
    def test_graph_mode_registers_exactly_the_declared_names(self) -> None:
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


class ModeNamesAreOneContract(unittest.TestCase):
    def test_every_mode_names_a_prompt_file_that_exists(self) -> None:
        """`agent_instructions()` resolves a mode to `prompts/<mode>.md`.

        A mode whose file was renamed on only one side of that mapping raises
        FileNotFoundError on the first turn of a conversation and nowhere
        earlier, so the read is done here instead.
        """
        # `_read` is cached on the file name, and an earlier test in the same
        # process would otherwise satisfy this one from memory.
        _read.cache_clear()
        instructions: dict[str, str] = {}
        for mode in MODES:
            with self.subTest(mode=mode):
                environment = dict(MCP_ENVIRONMENT, MCP_MODE=mode)
                with patch.dict("os.environ", environment, clear=True):
                    instructions[mode] = agent_instructions()
                self.assertTrue(instructions[mode])
        # Existing is not the same as being the right file: two modes reading
        # the same text means one of them is describing the other's tools.
        # Which mode is wired to which file is not checkable from a loop over
        # `MODES`, but distinctness is, and it survives a third mode.
        self.assertEqual(
            len(set(instructions.values())),
            len(MODES),
            "two modes are given the same instructions",
        )

    def test_every_mode_names_a_tool_set(self) -> None:
        """The same lookup, for the allow-list the chat app hands the agent.

        `ToolNamesAreOneContract` above names both modes by hand; this one
        follows `MODES`, so a third mode added without a tool set fails here.
        """
        sets: dict[str, frozenset[str]] = {}
        for mode in MODES:
            with self.subTest(mode=mode):
                environment = dict(
                    MCP_ENVIRONMENT, MCP_MODE=mode, **DATABRICKS_ENVIRONMENT
                )
                with patch.dict("os.environ", environment, clear=True):
                    sets[mode] = allowed_tools()
                self.assertTrue(sets[mode])
        # And a mode wired to another mode's set is non-empty too, while the
        # agent is handed names its server never registered.
        self.assertEqual(
            len(set(sets.values())), len(MODES), "two modes share a tool set"
        )

    def test_the_frontend_renders_exactly_the_modes_the_code_knows(self) -> None:
        """A mode missing from `modes` leaves the page blank for that mode alone.

        `modes[mode]` is undefined, and nothing on the way there throws.
        """
        source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        block = FRONTEND_MODES.search(source)
        self.assertIsNotNone(block, "no `const modes` object found in App.tsx")
        keys = FRONTEND_MODE_KEY.findall(block.group(1))
        # As a set: the order of keys in an object literal carries nothing.
        self.assertEqual(set(keys), set(MODES))
