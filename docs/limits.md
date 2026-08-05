# What this sample deliberately does not do

This sample demonstrates a delegated identity pattern. It is not a hardened
deployment. The limits below are intentional, and most of them are things a
production deployment would have to add. Read this page before adapting the
sample.

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

## One replica, and sessions in its memory

Web sessions live in that pod's memory, capped at 2000, and end after 30 minutes
idle or 8 hours from sign-in. A pod restart signs everyone out, and a second
replica would break sessions outright.

## Generic MCP clients cannot sign in

The server half of the MCP authorization spec is implemented: `/mcp` serves RFC
9728 protected resource metadata and answers an unauthenticated call with a
`WWW-Authenticate` challenge carrying `resource_metadata`, so a client can
discover which authorization server to go to. It still cannot finish, for two
reasons that belong to Entra rather than to this repo:

- The spec says a client MUST send the RFC 8707 `resource` parameter naming the
  canonical MCP server URI. Entra does not implement resource indicators. The
  audience comes from the scope, which here is
  `api://<server-app-id>/Mcp.Tools.ReadWrite` and can never equal the server's
  URL.
- Entra's OIDC metadata omits `code_challenge_methods_supported`, and the spec
  tells a client that finds it missing to refuse to proceed.

So authorization works only for clients registered against the server
application in advance: the chat app, and the Azure CLI when
`HARNESS_CLIENT_APP_ID` pre-authorizes it. Point a generic MCP client at `/mcp`
and it fails during discovery, not at the token endpoint.

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
