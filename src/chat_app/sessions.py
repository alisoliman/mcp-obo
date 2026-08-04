"""Browser sessions: the one place this application decides who is asking.

Everything downstream -- the MCP server, the OBO exchange, Unity Catalog --
correctly believes whatever this module concludes, so the session identifier and
its lifetime are the whole of the trust decision on the browser side.

The store is per-pod and in memory. That is a deliberate limit rather than an
oversight, and docs/limits.md says what it costs.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

if TYPE_CHECKING:
    from agent_framework import AgentSession

# The `__Host-` prefix makes the browser refuse the cookie unless it was set
# with Secure, Path=/ and no Domain, which is the one control that survives the
# DNS shape this accelerator ships: `cloudapp.azure.com` is not on the Public
# Suffix List (only `cloudapp.net` is), so a sibling Azure DNS label in the same
# region can otherwise set a Domain-scoped cookie of this name on us.
COOKIE_NAME = "__Host-order_agent_session"
# Idle timeout, refreshed on each request, and a hard ceiling measured from the
# moment the user actually signed in. Without the ceiling a stolen cookie stays
# usable for as long as MSAL can keep refreshing, which outlives whatever
# sign-in frequency the tenant's Conditional Access policy asked for.
SESSION_IDLE_TTL_SECONDS = 30 * 60
SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60
# A session that /login created but Entra has not yet returned to only has to
# survive one round trip, and it is the one an unauthenticated caller can mint.
# Ten minutes rather than something tighter because that is how long Entra keeps
# the authorization code alive: expiring first would fail a sign-in that is
# still perfectly valid, which is what a user pausing on an MFA prompt looks
# like.
PRE_AUTH_TTL_SECONDS = 10 * 60
# The store is per-pod and in memory, so an unauthenticated GET /login loop is
# an allocation primitive. This is a ceiling, not capacity planning.
MAX_SESSIONS = 2_000


@dataclass
class BrowserSession:
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    token_cache: str = ""
    auth_flow: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    agent_session: AgentSession | None = None
    # None until the authorization code has been redeemed. Both the absolute
    # ceiling and the pre-authentication TTL key off this, so it is the flag for
    # "this session has been through Entra" as much as it is a timestamp.
    authenticated_at: float | None = None
    expires_at: float = field(
        default_factory=lambda: time.time() + PRE_AUTH_TTL_SECONDS
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def authenticate(self, now: float) -> None:
        self.authenticated_at = now
        self.expires_at = now + SESSION_IDLE_TTL_SECONDS

    def touch(self, now: float) -> None:
        """Slide the idle deadline, but never past the absolute one.

        A pre-authentication session is deliberately not slid: it has one job,
        and letting an unauthenticated caller keep it alive by polling would
        hand back the unbounded lifetime this is here to remove.
        """
        if self.authenticated_at is None:
            return
        self.expires_at = min(
            now + SESSION_IDLE_TTL_SECONDS,
            self.authenticated_at + SESSION_ABSOLUTE_TTL_SECONDS,
        )


class SessionStore:
    """Live sessions for one pod, swept and capped on insert."""

    def __init__(self, *, max_sessions: int | None = None) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._max_sessions = MAX_SESSIONS if max_sessions is None else max_sessions

    def __len__(self) -> int:
        return len(self._sessions)

    def add(self, session_id: str, session: BrowserSession, now: float) -> None:
        """Insert, sweeping the expired and capping what is left.

        Sweeping on insert is enough because insert is the only unauthenticated
        path into this dict; evicting the soonest-to-expire first drops the
        pre-authentication shells a flood creates, which carry the shortest TTL
        of anything here, before it touches anyone who actually signed in.
        """
        sessions = self._sessions
        for stale_id in [sid for sid, s in sessions.items() if s.expires_at <= now]:
            sessions.pop(stale_id, None)
        while len(sessions) >= self._max_sessions:
            sessions.pop(min(sessions, key=lambda sid: sessions[sid].expires_at), None)
        sessions[session_id] = session

    def get(self, session_id: str, now: float) -> BrowserSession | None:
        """The live session for this id, its idle deadline slid forward."""
        session = self._sessions.get(session_id)
        if session is None or session.expires_at <= now:
            self._sessions.pop(session_id, None)
            return None
        session.touch(now)
        return session

    def pop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def set_session_cookie(response: Response, session_id: str, *, max_age: int) -> None:
    # Entra returns the authorization code as a cross-site form POST, which a
    # SameSite=Lax cookie would not accompany. Every state-changing route checks
    # the CSRF header instead of relying on SameSite. Secure and Path=/ with no
    # Domain are what the `__Host-` prefix requires; a browser that sees the
    # prefix without them drops the cookie entirely.
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )


def clear_session_cookie(response: Response) -> None:
    # The deletion cookie has to satisfy the `__Host-` rules too, otherwise the
    # browser rejects it and the session cookie survives the logout.
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )
