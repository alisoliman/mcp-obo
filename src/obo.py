"""The on-behalf-of exchange, shared by every downstream resource.

This server holds no secret. Its credential is the AKS Workload Identity token
projected into the pod, which stands in for a client secret when Entra ID asks
the server to prove which application it is.

What makes the exchange *delegated* rather than application-only is the second
assertion: the caller's own access token. Entra returns a token for the
requested resource that still carries the user, so the downstream service
authorizes the person who asked, not the server that relayed the question.

Swapping the `scope` argument is the entire difference between reaching
Microsoft Graph and reaching Azure Databricks. Everything else about the
exchange -- the workload assertion, the user assertion, the absence of a stored
credential -- is identical, which is the point of keeping it in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from azure.identity.aio import OnBehalfOfCredential

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# The Azure Databricks resource application. This app id is a fixed Azure
# constant -- the same in every tenant -- and `user_impersonation` is its only
# delegated permission. Asking for `/.default` requests whatever delegated
# permissions this application has been consented, which infra/main.bicep pins
# to exactly that one scope.
DATABRICKS_RESOURCE_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DATABRICKS_SCOPE = f"{DATABRICKS_RESOURCE_APP_ID}/.default"


@dataclass(frozen=True)
class OboExchange:
    """Trades a caller's token for a downstream token that is still theirs."""

    tenant_id: str
    client_id: str
    assertion: Callable[[], str]

    async def token(self, user_assertion: str, scope: str) -> str:
        # A credential per exchange keeps the flow obvious and holds no state
        # between callers. Add caching only if demo traffic warrants it; doing
        # so would mean keying the cache by user, which is easy to get wrong.
        credential = OnBehalfOfCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_assertion_func=self.assertion,
            user_assertion=user_assertion,
        )
        try:
            return (await credential.get_token(scope)).token
        finally:
            await credential.close()
