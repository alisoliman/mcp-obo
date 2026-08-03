"""The on-behalf-of exchange, shared by every downstream resource.

This server holds no secret. Its credential is the AKS Workload Identity token
projected into the pod, which stands in for a client secret. The second
assertion -- the caller's own access token -- is what makes the exchange
delegated: Entra returns a token that still carries the user, so the downstream
service authorizes the person who asked rather than the server that relayed the
question. Swapping `scope` is the entire difference between Microsoft Graph and
Azure Databricks.

The exchange is the handful of arguments in `_new_credential`. The rest of this
file is a cache, so one tool call is not one round trip to Entra, and an error
that survives being masked, so an operator can tell a Conditional Access policy
from a bug.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from base64 import urlsafe_b64decode
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import OnBehalfOfCredential

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# A fixed Azure constant, the same in every tenant. `/.default` asks for
# whatever delegated permissions this application has been consented, which
# infra/main.bicep pins to `user_impersonation` alone.
DATABRICKS_RESOURCE_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DATABRICKS_SCOPE = f"{DATABRICKS_RESOURCE_APP_ID}/.default"

# Entra was the last unbounded dependency in this process: azure-core defaults
# to a 300 second connect, a 300 second read and ten retries, so a token
# endpoint that hangs would hold a request open long past the point where every
# other hop -- the Databricks client, the SQL statement, the chat turn -- has
# given up. A token request is a small POST to a global service; if it has not
# answered in fifteen seconds it is not going to.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 15.0
_RETRY_TOTAL = 2

# One live credential per caller token. Caching is not an optimisation to add
# later: Entra's resilience guidance names a middle tier that re-exchanges on
# every call as the usual cause of 429s. The key is not a judgement call
# either -- azure-identity locks each credential to a single assertion ("each
# instance of this class is locked to a single user"), and MSAL keys its own
# OBO cache by the hash of that same assertion when none is given. Hashing it
# here matches the layer underneath instead of inventing a scheme.
_CACHE_LIMIT = 32

# Stop serving an entry a minute before its assertion expires, so a credential
# is not handed out and then refused halfway through a tool call.
_EXPIRY_SKEW = 60.0

_AADSTS_CODE = re.compile(r"AADSTS(\d+)")
_ENTRA_ERROR = re.compile(r"Entra ID error '\(([a-zA-Z_]+)\)")


class OboError(RuntimeError):
    """An Entra refusal, named precisely enough for someone to act on it.

    The failures that matter here are configuration, not code: a Conditional
    Access policy applied to the downstream resource (AADSTS50076), consent
    that was never granted (AADSTS65001), an assertion that has been revoked.
    Each has a different fix and none of them is a bug in this server, so the
    code and the AADSTS numbers travel with the exception rather than being
    flattened into prose.

    `claims` carries Entra's claims challenge when there is one. Relaying it
    back to the client is what would let the user satisfy the policy that is in
    place; that relay is not built here, so the value is preserved and nothing
    more, which is still better than dropping the one field that says the
    failure is recoverable.
    """

    def __init__(
        self,
        message: str,
        *,
        error: str = "",
        error_codes: tuple[int, ...] = (),
        claims: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.error_codes = error_codes
        self.claims = claims


@dataclass
class _CachedCredential:
    """A shared credential, and the count of callers currently inside it.

    Closing an `OnBehalfOfCredential` closes its transport, so whoever removes
    an entry from the cache cannot also be the one to close it -- an eviction
    happens on someone else's request, and the caller being evicted out from
    under is usually holding a perfectly good assertion. `users` is what lets
    the last one out turn off the lights.
    """

    credential: OnBehalfOfCredential
    expires_at: float
    users: int = 0
    retired: bool = False


@dataclass(frozen=True)
class OboExchange:
    """Trades a caller's token for a downstream token that is still theirs."""

    tenant_id: str
    client_id: str
    assertion: Callable[[], str]
    _cache: OrderedDict[str, _CachedCredential] = field(
        default_factory=OrderedDict, compare=False, repr=False
    )
    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, compare=False, repr=False
    )

    async def token(self, user_assertion: str, scope: str) -> str:
        key = sha256(user_assertion.encode("utf-8")).hexdigest()
        entry = await self._checkout(key, user_assertion)
        try:
            return (await entry.credential.get_token(scope)).token
        except ClientAuthenticationError as cause:
            # Whatever Entra just refused it will refuse again, and this
            # credential still holds the downstream token it minted before that
            # happened. Retiring takes it out of the cache without closing it
            # under a concurrent caller; the last one out does that.
            await self._retire(key, entry)
            raise _entra_refusal(cause) from cause
        except Exception as cause:
            # Only the exception type travels out: unlike Entra's error body, an
            # arbitrary message has not been vetted for what it might name.
            raise OboError(
                "The on-behalf-of exchange failed before Entra ID answered "
                f"({type(cause).__name__})"
            ) from cause
        finally:
            # Not `async with`: the credential outlives this call when the cache
            # keeps it, so closing is the reference count's job, not the block's.
            await self._release(entry)

    def _new_credential(self, user_assertion: str) -> OnBehalfOfCredential:
        return OnBehalfOfCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_assertion_func=self.assertion,
            user_assertion=user_assertion,
            connection_timeout=_CONNECT_TIMEOUT,
            read_timeout=_READ_TIMEOUT,
            retry_total=_RETRY_TOTAL,
        )

    async def _checkout(self, key: str, user_assertion: str) -> _CachedCredential:
        """An entry for this assertion, with this caller counted as a user."""
        expires_at = _assertion_expiry(user_assertion)
        now = time.time()
        stale: list[_CachedCredential] = []
        async with self._lock:
            entry = self._cache.pop(key, None)
            if entry is not None and entry.expires_at > now:
                self._cache[key] = entry
                entry.users += 1
                return entry
            if entry is not None:
                stale.append(entry)

            # An assertion whose lifetime cannot be read is used once and
            # dropped rather than cached on a guess about how long the caller's
            # token stays valid. It is still an entry, so one code path owns the
            # lifetime of every credential; it is simply retired from birth.
            usable_until = (expires_at or 0.0) - _EXPIRY_SKEW
            cacheable = usable_until > now
            fresh = _CachedCredential(
                self._new_credential(user_assertion),
                usable_until,
                users=1,
                retired=not cacheable,
            )
            if cacheable:
                self._cache[key] = fresh
                while len(self._cache) > _CACHE_LIMIT:
                    _, evicted = self._cache.popitem(last=False)
                    stale.append(evicted)

        await self._discard(stale)
        return fresh

    async def _retire(self, key: str, entry: _CachedCredential) -> None:
        """Take an entry out of the cache without closing it under anyone."""
        async with self._lock:
            if self._cache.get(key) is entry:
                del self._cache[key]
            entry.retired = True

    async def _release(self, entry: _CachedCredential) -> None:
        async with self._lock:
            entry.users -= 1
            closing = entry.retired and entry.users <= 0
        if closing:
            await entry.credential.close()

    async def _discard(self, entries: list[_CachedCredential]) -> None:
        """Retire evicted entries, closing only the ones nobody is inside."""
        closing = []
        async with self._lock:
            for entry in entries:
                entry.retired = True
                if entry.users <= 0:
                    closing.append(entry.credential)
        await _close_all(closing)

    async def close(self) -> None:
        """Release every cached credential's transport. For tests and shutdown."""
        async with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
        await self._discard(entries)


async def _close_all(credentials: list[OnBehalfOfCredential]) -> None:
    for credential in credentials:
        await credential.close()


def _assertion_expiry(user_assertion: str) -> float | None:
    """The `exp` claim of the caller's token, or None if it cannot be read.

    The signature is deliberately not checked. `AzureJWTVerifier` validated this
    exact token -- signature, issuer, audience, scope -- before any tool body
    ran, and re-verifying here would only duplicate that gate a second time per
    call. What is read is not trusted either: `exp` decides nothing but how
    long a cache entry may live, and a token that cannot be parsed is simply
    not cached, because a cached credential outliving the assertion behind it
    would keep serving a downstream token after the caller's was revoked.
    """
    try:
        payload = user_assertion.split(".")[1]
        claims = json.loads(urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (LookupError, TypeError, ValueError):
        return None


def _entra_refusal(cause: ClientAuthenticationError) -> OboError:
    body = _entra_error_body(cause)
    message = str(body.get("error_description", "")) or str(cause)
    error = str(body.get("error", "")) or _first_group(_ENTRA_ERROR, str(cause))
    codes = tuple(
        int(code) for code in body.get("error_codes", []) or [] if _is_number(code)
    ) or tuple(int(code) for code in _AADSTS_CODE.findall(message))
    claims = body.get("claims")

    named = ", ".join(f"AADSTS{code}" for code in codes) or "no AADSTS code"
    # Entra's error body is safe to repeat: a token response is either an error
    # or a token, never both, and azure-identity scrubs `access_token` and
    # `refresh_token` from anything it echoes. The trace and correlation ids in
    # the description are exactly what a support case asks for.
    return OboError(
        f"Entra ID refused the on-behalf-of exchange: {error or 'unknown_error'} "
        f"({named}). {message[:400]}".strip(),
        error=error,
        error_codes=codes,
        claims=str(claims) if claims else None,
    )


def _entra_error_body(cause: ClientAuthenticationError) -> dict[str, Any]:
    """Entra's own JSON error, dug out of azure-identity's exception.

    azure-identity formats `error` and `error_description` into the message and
    drops the rest, but azure-core keeps the response object on the exception,
    so `error_codes` and the claims challenge are still reachable there. When
    they are not -- an exception raised before any response arrived -- the
    caller falls back to what the message says.
    """
    response = getattr(cause, "response", None)
    if response is None:
        return {}
    try:
        body = json.loads(response.text())
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _is_number(value: object) -> bool:
    return isinstance(value, int) or (isinstance(value, str) and value.isdigit())
