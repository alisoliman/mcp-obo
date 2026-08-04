"""Signing the user in, and holding their delegated token server-side.

The workload's own AKS identity signs the client assertion; the user's tokens
live in an MSAL cache attached to their browser session and never leave this
process. The browser gets a session cookie and a CSRF token, nothing else.
"""

from __future__ import annotations

import asyncio

from msal import ConfidentialClientApplication, SerializableTokenCache

from src.chat_app.sessions import BrowserSession
from src.chat_app.settings import Settings


def workload_assertion(settings: Settings) -> str:
    assertion = settings.token_file.read_text(encoding="utf-8").strip()
    if not assertion:
        raise RuntimeError("The projected workload identity token is empty")
    return assertion


def msal_app(
    settings: Settings,
    session: BrowserSession,
) -> tuple[ConfidentialClientApplication, SerializableTokenCache]:
    cache = SerializableTokenCache()
    if session.token_cache:
        cache.deserialize(session.token_cache)
    app = ConfidentialClientApplication(
        settings.client_id,
        authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
        client_credential={
            "client_assertion": lambda: workload_assertion(settings),
        },
        token_cache=cache,
    )
    return app, cache


def save_cache(session: BrowserSession, cache: SerializableTokenCache) -> None:
    if cache.has_state_changed:
        session.token_cache = cache.serialize()


async def delegated_token(settings: Settings, session: BrowserSession) -> str | None:
    """The signed-in user's token for the MCP server, refreshed if MSAL can."""
    app, cache = msal_app(settings, session)
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = await asyncio.to_thread(
        app.acquire_token_silent,
        [settings.mcp_scope],
        account=accounts[0],
    )
    save_cache(session, cache)
    if not result or "access_token" not in result:
        return None
    return str(result["access_token"])
