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

from src.shared.modes import DATABRICKS_MODE, current_mode
from src.shared.tools import DATABRICKS_TOOLS, ORDER_TOOLS

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def allowed_tools() -> frozenset[str]:
    """The tool names for the mode this deployment is running."""
    return DATABRICKS_TOOLS if current_mode() == DATABRICKS_MODE else ORDER_TOOLS


def agent_instructions() -> str:
    return _read("databricks" if current_mode() == DATABRICKS_MODE else "orders")


@cache
def _read(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"The {name} agent instructions at {path} are empty")
    return text
