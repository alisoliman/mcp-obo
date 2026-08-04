"""`python -m src.chat_app` -- the container entry point.

Kept apart from `app.py` so that importing the application, which every test
does, never reads a port or binds a socket.
"""

from __future__ import annotations

import os

import uvicorn

from src.chat_app.app import create_app

uvicorn.run(
    create_app(),
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8001")),
    access_log=False,
)
