"""What the browser is allowed to see of a turn.

Everything streamed to the browser passes through `safe_event_value` first. Tool
arguments and results are third-party data -- a Unity Catalog table comment, a
Graph display name -- and they are rendered in a page that holds a live session.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware
from pydantic import BaseModel

# A substring denylist over key names, applied to every tool result before it
# reaches the browser. Nothing in this repo's tools can return a credential —
# they return frozen models with no such field — so this exists for the tools an
# adopter bolts on. Treat it as a floor: a tool that returns a secret under a
# name nobody thought of is still a leak, and the fix is the tool, not this list.
SENSITIVE_EVENT_KEYS = (
    "_key",
    "apikey",
    "assertion",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "jwt",
    "passphrase",
    "password",
    "private",
    "sas",
    "secret",
    "session",
    "signature",
    "token",
)


def safe_event_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[truncated]"
    if isinstance(value, BaseModel):
        return safe_event_value(value.model_dump(mode="json"), depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if any(term in str(key).lower() for term in SENSITIVE_EVENT_KEYS)
                else safe_event_value(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_event_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 4_000 else f"{value[:4_000]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_event_value(str(value), depth=depth + 1)


def sse_event(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(
        safe_event_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


def sse_text_event(event: str, text: str) -> str:
    data = json.dumps(
        {"text": text},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


def tool_result_value(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    text_values = [
        item.text
        for item in value
        if getattr(item, "type", None) == "text" and getattr(item, "text", None)
    ]
    if not text_values:
        return value
    for text in text_values:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return text_values[0] if len(text_values) == 1 else text_values


class ToolEventMiddleware(FunctionMiddleware):
    def __init__(self, events: asyncio.Queue) -> None:
        self.events = events

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        event_id = secrets.token_urlsafe(9)
        await self.events.put(
            (
                "tool.started",
                {
                    "id": event_id,
                    "name": context.function.name,
                    "input": context.arguments,
                },
            )
        )
        try:
            await call_next()
        except Exception:
            await self.events.put(
                (
                    "tool.completed",
                    {
                        "id": event_id,
                        "name": context.function.name,
                        "status": "error",
                        "output": "Tool call failed.",
                    },
                )
            )
            raise
        await self.events.put(
            (
                "tool.completed",
                {
                    "id": event_id,
                    "name": context.function.name,
                    "status": "completed",
                    "input": context.arguments,
                    "output": tool_result_value(context.result),
                },
            )
        )
