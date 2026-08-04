"""The credential cache, and the Entra refusals it has to survive."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from azure.core.exceptions import ClientAuthenticationError

from src.mcp_server import obo
from src.mcp_server.obo import (
    GRAPH_SCOPE,
    OboError,
)
from tests.helpers import (
    MCP_ENVIRONMENT,
    user_assertion,
)


class FakeOboCredential:
    """Stands in for OnBehalfOfCredential, counting what the cache does to it.

    The exchange itself is Entra's answer to a real request and cannot happen
    here. Everything around it can: how many credentials get built, which
    assertion each one is locked to, and whether the transport is released.
    """

    instances: list[FakeOboCredential] = []
    refusal: Exception | None = None
    # Seconds get_token suspends for. Zero for the sequential tests; a real
    # delay is what lets a concurrent test hold a caller inside the exchange
    # while the cache is disturbed underneath it.
    delay: float = 0.0

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        # A count rather than a flag: closing a transport twice is its own bug,
        # and the whole point of the reference count in src/mcp_server/obo.py is
        # that only the last caller out does it.
        self.closed = 0
        FakeOboCredential.instances.append(self)

    async def get_token(self, scope: str) -> SimpleNamespace:
        if FakeOboCredential.delay:
            await asyncio.sleep(FakeOboCredential.delay)
        if self.closed:
            raise RuntimeError("transport closed under an in-flight caller")
        if FakeOboCredential.refusal is not None:
            raise FakeOboCredential.refusal
        return SimpleNamespace(token=f"downstream-token-for-{scope}")

    async def close(self) -> None:
        self.closed += 1


class OboExchangeTests(unittest.TestCase):
    """The cache and the refusal, checked without Entra.

    Nothing here proves the exchange is delegated -- that is Entra's answer to a
    real request, and `tests/e2e_databricks.py` is where it shows. What a unit
    test can hold is what the cache must never get wrong: one caller's
    credential is never handed to another, and an entry never outlives the
    assertion it was built from.
    """

    def setUp(self) -> None:
        FakeOboCredential.instances = []
        FakeOboCredential.refusal = None

    def exchange(self) -> obo.OboExchange:
        return obo.OboExchange(
            tenant_id=MCP_ENVIRONMENT["AZURE_TENANT_ID"],
            client_id=MCP_ENVIRONMENT["ENTRA_SERVER_CLIENT_ID"],
            assertion=lambda: "workload-identity-token",
        )

    def run_exchange(self, work: Any) -> Any:
        with patch("src.mcp_server.obo.OnBehalfOfCredential", FakeOboCredential):
            return asyncio.run(work())

    def test_a_cached_credential_is_never_shared_between_callers(self) -> None:
        exchange = self.exchange()
        ada, grace = user_assertion("ada"), user_assertion("grace")

        async def work() -> None:
            await exchange.token(ada, GRAPH_SCOPE)
            await exchange.token(ada, GRAPH_SCOPE)
            await exchange.token(grace, GRAPH_SCOPE)
            await exchange.close()

        self.run_exchange(work)

        # Two credentials for three calls. azure-identity locks each one to a
        # single assertion, so the only safe reuse is within one caller, and the
        # key is the hash of that assertion for exactly that reason.
        self.assertEqual(len(FakeOboCredential.instances), 2)
        self.assertEqual(
            [
                credential.kwargs["user_assertion"]
                for credential in FakeOboCredential.instances
            ],
            [ada, grace],
        )
        # Closing is what releases the transport, so a cache that dropped an
        # entry without closing it would leak a connection pool per caller.
        self.assertTrue(
            all(credential.closed for credential in FakeOboCredential.instances)
        )

    def test_an_assertion_that_cannot_be_dated_is_used_once_and_dropped(self) -> None:
        # A cached credential outliving the assertion behind it would keep
        # serving a downstream token after the caller's was revoked, so anything
        # whose remaining lifetime cannot be read is used once and closed.
        uncacheable = {
            "not a JWT at all": "opaque-token",
            "already expired": user_assertion(expires_in=-60),
            "expiring inside the skew": user_assertion(expires_in=obo._EXPIRY_SKEW / 2),
        }
        for description, assertion in uncacheable.items():
            with self.subTest(assertion=description):
                FakeOboCredential.instances = []
                exchange = self.exchange()

                async def work(exchange=exchange, assertion=assertion) -> None:
                    await exchange.token(assertion, GRAPH_SCOPE)
                    await exchange.token(assertion, GRAPH_SCOPE)

                self.run_exchange(work)

                self.assertEqual(len(FakeOboCredential.instances), 2)
                self.assertTrue(
                    all(credential.closed for credential in FakeOboCredential.instances)
                )
                self.assertFalse(exchange._cache)

    def test_the_cache_is_bounded_and_closes_what_it_evicts(self) -> None:
        exchange = self.exchange()

        async def work() -> None:
            for index in range(obo._CACHE_LIMIT + 4):
                await exchange.token(user_assertion(f"user-{index}"), GRAPH_SCOPE)

        self.run_exchange(work)

        # The cache is keyed by caller and the number of callers a pod sees is
        # not something this process decides, so the bound is the only thing
        # standing between a busy deployment and one credential per user.
        self.assertEqual(len(exchange._cache), obo._CACHE_LIMIT)
        self.assertTrue(
            all(credential.closed for credential in FakeOboCredential.instances[:4])
        )
        self.assertFalse(
            any(credential.closed for credential in FakeOboCredential.instances[4:])
        )

    def test_a_refused_credential_is_dropped_rather_than_served_again(self) -> None:
        exchange = self.exchange()
        assertion = user_assertion()

        async def work() -> None:
            await exchange.token(assertion, GRAPH_SCOPE)
            FakeOboCredential.refusal = ClientAuthenticationError(
                "Microsoft Entra ID error '(invalid_grant) AADSTS50173: The "
                "provided grant has expired due to it being revoked.'"
            )
            with self.assertRaises(OboError):
                await exchange.token(assertion, GRAPH_SCOPE)
            FakeOboCredential.refusal = None
            await exchange.token(assertion, GRAPH_SCOPE)

        self.run_exchange(work)

        # Whatever Entra just refused it will refuse again, and the refused
        # credential is still holding the downstream token it minted before that
        # happened. Keeping the entry would hand that token to the next call.
        self.assertEqual(len(FakeOboCredential.instances), 2)
        self.assertTrue(FakeOboCredential.instances[0].closed)

    def test_an_eviction_does_not_close_a_credential_someone_is_inside(self) -> None:
        # The cache is shared and eviction happens on someone else's request, so
        # the caller evicted out from under is holding a perfectly good
        # assertion of their own. Closing the transport there fails their tool
        # call with a transport error the exchange cannot even name -- a
        # stranger's traffic breaking yours, which is the worst shape a caching
        # bug can take in a multi-user middle tier.
        exchange = self.exchange()

        async def work() -> str:
            FakeOboCredential.delay = 0.02
            victim = asyncio.ensure_future(
                exchange.token(user_assertion("victim"), GRAPH_SCOPE)
            )
            await asyncio.sleep(0)
            FakeOboCredential.delay = 0.0
            for index in range(obo._CACHE_LIMIT + 4):
                await exchange.token(user_assertion(f"other-{index}"), GRAPH_SCOPE)
            return await victim

        try:
            token = self.run_exchange(work)
        finally:
            FakeOboCredential.delay = 0.0

        self.assertEqual(token, f"downstream-token-for-{GRAPH_SCOPE}")
        self.assertTrue(
            all(credential.closed <= 1 for credential in FakeOboCredential.instances)
        )

    def test_two_callers_on_one_refused_credential_both_hear_why(self) -> None:
        # Concurrent tool calls from one user share a cache entry. When Entra
        # refuses, both must get the refusal that names its fix -- if the first
        # closes the credential, the second's failure arrives as an unnamed
        # transport error instead, which is exactly the "Error calling tool"
        # opacity this pass set out to remove.
        exchange = self.exchange()
        assertion = user_assertion("ada")

        async def work() -> list[BaseException | str]:
            await exchange.token(assertion, GRAPH_SCOPE)
            FakeOboCredential.refusal = ClientAuthenticationError(
                "Microsoft Entra ID error '(invalid_grant) AADSTS50076: Due to a "
                "configuration change made by your administrator.'"
            )
            FakeOboCredential.delay = 0.02
            try:
                return await asyncio.gather(
                    exchange.token(assertion, GRAPH_SCOPE),
                    exchange.token(assertion, GRAPH_SCOPE),
                    return_exceptions=True,
                )
            finally:
                FakeOboCredential.refusal = None
                FakeOboCredential.delay = 0.0

        results = self.run_exchange(work)

        for result in results:
            self.assertIsInstance(result, OboError)
            self.assertEqual(result.error_codes, (50076,))
        self.assertFalse(exchange._cache)
        # Once, by whichever caller left last.
        self.assertEqual(FakeOboCredential.instances[0].closed, 1)

    def test_a_refusal_carries_the_code_that_names_its_fix(self) -> None:
        # The exact shape azure-identity raises when Entra answers with an
        # error and azure-core kept no response to read. Parsing it is the
        # difference between an operator seeing a Conditional Access policy and
        # seeing "Error calling tool".
        error = obo._entra_refusal(
            ClientAuthenticationError(
                "Microsoft Entra ID error '(invalid_grant) AADSTS50076: Due to a "
                "configuration change made by your administrator, you must use "
                "multi-factor authentication to access this resource. Trace ID: "
                "3f0b1c2d Correlation ID: 9a8b7c6d'"
            )
        )

        self.assertEqual(error.error, "invalid_grant")
        self.assertEqual(error.error_codes, (50076,))
        self.assertIn("invalid_grant", str(error))
        self.assertIn("AADSTS50076", str(error))
        # The trace and correlation ids are what a support case asks for, so
        # they survive into the message rather than being summarised away.
        self.assertIn("Trace ID: 3f0b1c2d", str(error))

    def test_a_claims_challenge_survives_the_refusal(self) -> None:
        # Entra's own error body, which azure-identity drops and azure-core
        # keeps on the response. `claims` is the one field that says the failure
        # is recoverable by the user rather than by an administrator.
        body = {
            "error": "interaction_required",
            "error_description": "AADSTS50076: multi-factor authentication required.",
            "error_codes": [50076],
            "claims": '{"access_token":{"capolids":{"essential":true}}}',
        }
        cause = ClientAuthenticationError("authentication failed")
        cause.response = SimpleNamespace(text=lambda: json.dumps(body))

        error = obo._entra_refusal(cause)

        self.assertEqual(error.error, "interaction_required")
        self.assertEqual(error.error_codes, (50076,))
        self.assertEqual(error.claims, body["claims"])
