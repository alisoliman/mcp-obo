# Delegated identity for MCP servers on Azure

An MCP server where every tool call runs as the person who asked — not as the
service. No secrets anywhere, and no application-permission path to user data.

Deployed with one `azd up` onto AKS, with a Microsoft Agent Framework chat app
in front of it.

## Pick a mode

The two are alternatives, not layers. Each is a complete example; you deploy one.

| | **orders** | **databricks** |
|---|---|---|
| For | Building your own MCP server on Azure | Reaching a governed data platform as the caller |
| Downstream | Microsoft Graph | Azure Databricks + Unity Catalog |
| Who authorizes | Your code, by the object id Graph returned | Unity Catalog row filters, column masks and grants |
| Extra infrastructure | None | A premium Databricks workspace |
| Read next | [orders mode](docs/orders-mode.md) | [databricks mode](docs/databricks-mode.md) |

The identity path is **identical** in both. The only difference is which scope
the on-behalf-of exchange asks Entra for:

```
orders      scope = https://graph.microsoft.com/.default
databricks  scope = 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default
```

That is `src/obo.py`, shared by both. The request itself is a handful of
arguments; the rest of the file is the credential cache and the typed Entra
error a middle tier cannot ship without.

## The one idea

The chat app authenticates the user and holds their delegated token. Every MCP
call carries that token. The MCP server verifies it, then exchanges it — along
with its own pod identity — for a token to a downstream resource that **still
carries the user**.

Neither workload can reach user data on its own. The MCP server holds only
delegated permissions, and a delegated permission is inert without a caller.

**[How the authorization actually works](docs/authorization.md)** explains this
properly: the three identities, the three gates, and why Microsoft Graph is not
the authorization system. Read it once and the rest of the repo follows.

## What runs where

<img src="docs/architecture.svg" alt="The browser reaches ingress-nginx on AKS, which routes / to the chat workload and /mcp to the FastMCP resource server. Green edges carry a user identity to Entra ID and the downstream resource; blue edges carry transport and Foundry inference." width="100%">

Green edges carry or establish a user identity. Blue edges do not — which is why
the arrow to Foundry is blue and the arrow between the two pods is green.

## Prerequisites

- Azure subscription Owner, or Contributor plus User Access Administrator.
- An Entra **Application Administrator**, Cloud Application Administrator,
  Privileged Role Administrator, or Global Administrator role, active. Directory
  roles travel inside the access token, so sign in again after activating one.
- At least 100K available Global Standard quota for `gpt-5.6-terra`.
- Azure CLI, Azure Developer CLI, `uv`, `kubectl`, `kubelogin`, and Helm 3.
- macOS, Linux, or WSL for the POSIX `azd` hooks.

No local Docker daemon, and no Databricks account administrator. Node.js 22 is
needed only when changing the frontend; ACR builds it remotely.

## Deploy

```bash
az login --tenant <tenant-id>
azd auth login --tenant-id <tenant-id>

azd env new demo
azd env set AZURE_SUBSCRIPTION_ID <subscription-id>
azd env set AZURE_LOCATION northeurope
azd env set MCP_MODE orders          # or: databricks
azd up
```

`azd up` provisions Foundry, the model, both app registrations, AKS, ACR,
networking, both images, the Helm dependencies and both workloads. In databricks
mode it also provisions the workspace and runs the Unity Catalog bootstrap.

Then open it:

```bash
azd env get-value APP_URL
```

Sign in and ask, depending on your mode:

```text
What orders do I have?
Prove which identity the MCP server resolved for me.
```

## Verify it

Before deploying — none of this needs Azure:

```bash
uv sync
uv run python -m unittest test_accelerator -v
az bicep build --file infra/main.bicep --stdout > /dev/null
cd frontend && npm ci && npm run build && npm run lint && cd ..
```

Against a live deployment:

```bash
azd provision --preview --no-prompt
uv run python -m unittest test_databricks_e2e -v   # databricks mode only
```

`test_accelerator` is named explicitly on purpose — bare `python -m unittest`
also discovers the end-to-end module, which calls the deployed stack, so the
fast local loop would quietly become live round trips once you have deployed.

The Python tests cover the order rules, per-user isolation, the mode switch, the
MCP token, scope and origin requirements, the tool annotations, the Databricks
host and identifier checks, the browser session and CSRF behaviour, and that the
compiled template requests no application permission. They do not touch Azure,
so nothing here proves a deployment works — only that the parts that can be
decided locally are decided correctly. Everything else is checked by the tool
that owns it: Bicep for the template, `azd provision --preview` for the plan,
and the two image builds for the Dockerfiles.

Note that `azd provision --preview` will not list the Databricks workspace — ARM
what-if has no coverage for that provider, so it omits those resources silently
rather than reporting them unchanged. Use `az bicep build` to confirm them.

## Source layout

| Path | Purpose |
|---|---|
| `src/obo.py` | The on-behalf-of exchange. Both modes call it; only `scope` differs |
| `src/server.py` | Token verification, and the mode switch |
| `src/orders.py` | orders mode: Graph OBO, authorization in the application |
| `src/databricks.py` | databricks mode: Databricks OBO, authorization in Unity Catalog |
| `src/chat.py` | Web login, safe session API, Agent Framework, delegated MCP calls |
| `scripts/bootstrap_databricks.py` | The Unity Catalog objects Bicep cannot create |
| `infra/main.bicep` | Azure, Foundry, Entra, consent, federation, and RBAC |
| `frontend/` | Vite React application with local shadcn UI |
| `k8s/` | Deployments, Services, issuer, and the shared Ingress |
| `test_accelerator.py` | Unit tests, no deployment needed |
| `test_databricks_e2e.py` | End-to-end proof against a live deployment |
| `docs/*.d2` | Diagram sources; the SVGs are committed alongside |

To follow the identity path in code, read the module docstring at the top of
`src/chat.py`, then `src/server.py`, then `src/obo.py`. Between them they
describe every token in the diagrams.

After editing a `.d2` file, re-render it:

```bash
for name in architecture token-flow trust-boundaries databricks-obo; do
  d2 "docs/$name.d2" "docs/$name.svg"
done
```

## Deliberate limits

- One replica for each workload. Web sessions live in that pod's memory, capped
  at 2000, and end after 30 minutes idle or 8 hours from sign-in. A pod restart
  signs everyone out, and a second replica would break sessions outright.
- Tool calls are auto-approved (`approval_mode="never_require"`), and the SSE
  stream is one-way, so there is nowhere for a user to answer an approval
  prompt. A delegated token turns prompt injection into an authorization
  problem: Unity Catalog can decide perfectly and the agent is still the
  confused deputy, because it acts under the user's own credential. What is in
  place: table names and comments are flattened to one capped line of plain data
  before the model sees them, the browser gets a CSP with `img-src 'self' data:`
  and `frame-ancestors 'none'`, rendered markdown URLs are restricted to
  http/https/mailto, and all three Databricks tools are annotated
  `readOnlyHint`. What is not in place is any human approval gate. Build one
  before pointing this at a tool that mutates anything.
- Generic MCP clients cannot authorize against this server through discovery;
  only clients registered in advance can. The reasons are Entra's and are
  spelled out in [the authorization
  model](docs/authorization.md#why-a-generic-mcp-client-cannot-sign-in-here).
- orders mode keeps orders in memory; they reset with the pod.
- databricks mode uses Databricks Default Storage and one 2X-Small SQL warehouse
  with a ten-minute auto-stop, so the first query after an idle period waits for
  it to start. The wait is bounded at 90 seconds, after which the tool says
  plainly that the warehouse is starting and to retry — a distinct message, not
  something that reads like a denial. Demo tables are dropped and reseeded on
  every bootstrap run.
- The Databricks workspace ships with public network access enabled, no IP
  access list and no Private Link. That is a demo default a customer deployment
  has to change.
- Both applications admit every account in the tenant, which is Entra's own
  default. `azd env set ALLOWED_PRINCIPAL_IDS <object-id>[,<object-id>...]` flips
  both service principals to requiring an app role assignment, with the deploying
  user assigned automatically so a locked-down deployment still admits its owner.
  A group object id needs Entra ID P1; user object ids work on any tier.
- Both workloads share the `orders` namespace, and Kubernetes has no "may use
  this ServiceAccount" permission, so anyone who can create a pod there can
  mount the MCP server's projected token and hold its Entra client assertion.
  Namespace RBAC is the control; the cluster has no network policy engine.
- No rate limiting beyond a coarse ingress floor of 20 requests per second with
  a large burst, keyed on the address nginx sees — which is often the node
  rather than the browser.
- Public demo DNS through the public IP's `cloudapp.azure.com` label. That
  domain is not on the Public Suffix List, so a sibling label in the same region
  can set a cookie on this host; the session cookie is `__Host-` prefixed and
  the session id is renewed at sign-in for that reason.
- No database, Redis, Key Vault, custom domain or private network, and no
  telemetry by default. `azd env set ENABLE_DATABRICKS_AUDIT_LOGS true` deploys a
  Log Analytics workspace and a diagnostic setting for the Databricks workspace's
  `unityCatalog`, `databrickssql` and `accounts` logs, which is what makes the
  audit-log claim checkable from outside Databricks. Log Analytics is billable.
- The transcript is not confined to the browser tab. `src/chat.py` creates an
  `AgentSession` per browser session and never sets `store`, and the Responses
  path behind Foundry returns and persists a conversation id unless it is
  `False` — so governed rows, order ids, owner addresses, amounts and the
  resolved identity become part of a Foundry-side conversation that is not row
  filtered on later access, is not in the Databricks audit log, and is not
  deleted at logout.

`azd deploy` writes an `images:` block into both `k8s/*/kustomization.yaml`
files naming the tag it just built. Those edits are local to your deployment and
are not committed.

## Clean up

```bash
azd down
```

The resource group holds the billable AKS, ACR, public IP, Foundry resource,
project, model deployment, and — in databricks mode — the workspace. `azd down`
does not remove Unity Catalog objects, which live in the metastore rather than
the resource group. Microsoft Graph application objects are tenant-scoped and
can outlive the resource group; remove the two generated app registrations if
`azd down` leaves them.

## Troubleshooting

**Bicep cannot create an app or permission grant** — activate an Entra
application-management role, run `az login` again to pick it up, and retry.

**The model deployment reports insufficient quota** — check the
`OpenAI.GlobalStandard.gpt-5.6-terra` quota in Sweden Central. The template
requests capacity 100.

**AKS deployment is forbidden** — `kubectl auth can-i create deployments.apps
--all-namespaces`. The deploying user needs Azure Kubernetes Service RBAC
Cluster Admin; Bicep assigns it, but propagation takes a few minutes.

**The certificate stays pending** — `kubectl describe certificate order-agent-tls
-n orders`, then `kubectl logs -n cert-manager deployment/cert-manager`.

**Chat works but tools fail** — `kubectl logs -n orders deployment/order-agent-chat`
and `kubectl logs -n orders deployment/fastmcp-orders`. Check both service
accounts carry the expected Workload Identity annotations.

Databricks-specific failures are in [databricks mode](docs/databricks-mode.md).

## References

- [Microsoft identity platform OAuth 2.0 On-Behalf-Of flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
- [Microsoft Agent Framework with Microsoft Foundry](https://learn.microsoft.com/agent-framework/agents/providers/microsoft-foundry)
- [Azure Developer CLI Helm and Kustomize support](https://learn.microsoft.com/azure/developer/azure-developer-cli/helm-kustomize-support)
- [Row filters and column masks in Unity Catalog](https://learn.microsoft.com/azure/databricks/data-governance/unity-catalog/filters-and-masks/)
