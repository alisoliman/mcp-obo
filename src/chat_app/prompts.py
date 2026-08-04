"""What the agent is told, and which tools it may call.

Both are per-mode, and both are read from the mode rather than passed in, so a
deployment cannot end up running one mode's tools under the other's
instructions.

The instruction text lives in `prompts/*.md` rather than in this file. It is the
first thing most people adapting this accelerator will want to change, and a
prompt buried in the middle of a Python module is not findable by someone who
came here to change the prompt.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from src.shared.modes import DATABRICKS_MODE, GRAPH_MODE, current_mode
from src.shared.tools import DATABRICKS_TOOLS, ORDER_TOOLS

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# Keyed rather than branched, so a mode added to `MODES` without a tool set
# raises here instead of silently being served another mode's.
_TOOLS = {GRAPH_MODE: ORDER_TOOLS, DATABRICKS_MODE: DATABRICKS_TOOLS}


def allowed_tools() -> frozenset[str]:
    """The tool names for the mode this deployment is running."""
    return _TOOLS[current_mode()]


def agent_instructions() -> str:
    # The file is named for the mode, so the mode is the whole lookup. A mode
    # with no `prompts/<mode>.md` raises FileNotFoundError rather than falling
    # through to whichever branch happened to be last.
    return _read(current_mode())


@cache
def _read(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"The {name} agent instructions at {path} are empty")
    return text
