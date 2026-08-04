"""Which downstream resource this deployment was built for.

Both workloads read `MCP_MODE`, and they have to agree: the server registers one
mode's tools and the chat app names that same mode to the browser and picks the
matching instructions. Keeping the resolution here rather than in either one is
what stops the two from disagreeing about what is deployed.
"""

from __future__ import annotations

import os

GRAPH_MODE = "graph"
DATABRICKS_MODE = "databricks"
MODES = (GRAPH_MODE, DATABRICKS_MODE)


def current_mode() -> str:
    mode = os.getenv("MCP_MODE", GRAPH_MODE).strip().lower() or GRAPH_MODE
    if mode not in MODES:
        raise RuntimeError(f"MCP_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    return mode
