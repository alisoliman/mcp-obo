# What this sample deliberately does not do

Everything here is a decision, not an oversight. A demonstration of a delegated
identity pattern is not a hardened deployment, and the difference is worth
stating precisely rather than leaving for someone to discover in production.

Read this before adapting the sample. Several of these are the exact things you
would have to build.

## The agent has no approval gate

Tool calls are auto-approved (`approval_mode="never_require"`), and the SSE
stream is one-way, so there is nowhere for a user to answer an approval prompt.

A delegated token turns prompt injection into an authorization problem: Unity
Catalog can decide perfectly and the agent is still the confused deputy, because
it acts under the user's own credential.

What is in place: table names and comments are flattened to one capped line of
plain data before the model sees them, the browser gets a CSP with
`img-src 'self' data:` and `frame-ancestors 'none'`, rendered markdown URLs are
restricted to http/https/mailto, and all three Databricks tools are annotated
`readOnlyHint`.

What is not in place is any human approval gate. Build one before pointing this
at a tool that mutates anything.

## The transcript is not confined to the browser tab

`src/chat_app/app.py` creates an `AgentSession` per browser session and never
sets `store`, and the Responses path behind Foundry returns and persists a
conversation id unless it is `False`.

So governed rows, order ids, owner addresses, amounts and the resolved identity
become part of a Foundry-side conversation that is not row filtered on later
access, is not in the Databricks audit log, and is not deleted at logout.

## One replica, and sessions in its memory

Web sessions live in that pod's memory, capped at 2000, and end after 30 minutes
idle or 8 hours from sign-in. A pod restart signs everyone out, and a second
replica would break sessions outright.

## Generic MCP clients cannot sign in

Only clients registered in advance can authorize against this server; discovery
is not enough. The reasons are Entra's and are spelled out in
[the authorization model](authorization.md#why-a-generic-mcp-client-cannot-sign-in-here).

## Both workloads share one namespace

Kubernetes has no "may use this ServiceAccount" permission, so anyone who can
create a pod in the `orders` namespace can mount the MCP server's projected
token and hold its Entra client assertion. Namespace RBAC is the control; the
cluster has no network policy engine.

## The Databricks workspace is open to the network

It ships with public network access enabled, no IP access list and no Private
Link. That is a demo default a customer deployment has to change.

## Everyone in the tenant is admitted

Both applications admit every account in the tenant, which is Entra's own
default.

```bash
azd env set ALLOWED_PRINCIPAL_IDS <object-id>[,<object-id>...]
```

flips both service principals to requiring an app role assignment, with the
deploying user assigned automatically so a locked-down deployment still admits
its owner. A group object id needs Entra ID P1; user object ids work on any
tier.

## Rate limiting is a coarse ingress floor

20 requests per second with a large burst, keyed on the address nginx sees --
which is often the node rather than the browser.

## Public demo DNS

Through the public IP's `cloudapp.azure.com` label. That domain is not on the
Public Suffix List, so a sibling label in the same region can set a cookie on
this host; the session cookie is `__Host-` prefixed and the session id is
renewed at sign-in for that reason.

## Data that resets

- graph mode keeps orders in memory; they reset with the pod.
- databricks mode drops and reseeds the demo tables on every bootstrap run.

## The warehouse sleeps

databricks mode uses Databricks Default Storage and one 2X-Small SQL warehouse
with a ten-minute auto-stop, so the first query after an idle period waits for
it to start. The wait is bounded at 90 seconds, after which the tool says
plainly that the warehouse is starting and to retry -- a distinct message, not
something that reads like a denial.

## No supporting services

No database, Redis, Key Vault, custom domain or private network, and no
telemetry by default.

```bash
azd env set ENABLE_DATABRICKS_AUDIT_LOGS true
```

deploys a Log Analytics workspace and a diagnostic setting for the Databricks
workspace's `unityCatalog`, `databrickssql` and `accounts` logs, which is what
makes the audit-log claim checkable from outside Databricks. Log Analytics is
billable.
