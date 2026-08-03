"""The on-behalf-of exchange, shared by every downstream resource.

This server holds no secret. Its credential is the AKS Workload Identity token
projected into the pod, which stands in for a client secret. The second
assertion -- the caller's own access token -- is what makes the exchange
delegated: Entra returns a token that still carries the user, so the downstream
service authorizes the person who asked rather than the server that relayed the
question. Swapping `scope` is the entire difference between Microsoft Graph and
Azure Databricks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from azure.identity.aio import OnBehalfOfCredential

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# A fixed Azure constant, the same in every tenant. `/.default` asks for
# whatever delegated permissions this application has been consented, which
# infra/main.bicep pins to `user_impersonation` alone.
DATABRICKS_RESOURCE_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DATABRICKS_SCOPE = f"{DATABRICKS_RESOURCE_APP_ID}/.default"


@dataclass(frozen=True)
class OboExchange:
    """Trades a caller's token for a downstream token that is still theirs."""

    tenant_id: str
    client_id: str
    assertion: Callable[[], str]

    async def token(self, user_assertion: str, scope: str) -> str:
        # A credential per exchange holds no state between callers. Caching
        # would have to be keyed by user, which is easy to get wrong.
        async with OnBehalfOfCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_assertion_func=self.assertion,
            user_assertion=user_assertion,
        ) as credential:
            return (await credential.get_token(scope)).token
