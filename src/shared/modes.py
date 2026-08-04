"""Which downstream resource this deployment was built for.

Both workloads read `MCP_MODE`, and they have to agree: the server registers one
mode's tools and the chat app names that same mode to the browser and picks the
matching instructions. Keeping the resolution here rather than in either one is
what stops the two from disagreeing about what is deployed.
"""

from __future__ import annotations

import os

ORDERS_MODE = "orders"
DATABRICKS_MODE = "databricks"
MODES = (ORDERS_MODE, DATABRICKS_MODE)


def current_mode() -> str:
    mode = os.getenv("MCP_MODE", ORDERS_MODE).strip().lower() or ORDERS_MODE
    if mode not in MODES:
        raise RuntimeError(f"MCP_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    return mode
