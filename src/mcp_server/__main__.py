"""`python -m src.mcp_server` -- the container entry point.

Kept apart from `server.py` so that importing the server, which every test does,
never reads a port or binds a socket.
"""

from __future__ import annotations

import os

from starlette.middleware import Middleware

from src.mcp_server.server import (
    MCP_PATH,
    SameOriginOnly,
    allowed_origins,
    create_server,
)

create_server().run(
    transport="http",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
    path=MCP_PATH,
    middleware=[Middleware(SameOriginOnly, allowed=allowed_origins())],
    show_banner=False,
)
