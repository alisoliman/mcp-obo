# Troubleshooting a deployment

What this sample deliberately does not do is in [limits](limits.md). Check there
before filing a bug, because several of the behaviours people report are
documented decisions.

## Bicep cannot create an app or permission grant

Activate an Entra application-management role, run `az login` again to pick it
up, and retry. Directory roles travel inside the access token, so activating one
does nothing until you sign in again.

The same applies when the on-behalf-of exchange itself is rejected: the server
application is missing the delegated permission or its consent, and re-running
`azd provision` fixes it only from a login that carries the role.

## The model deployment reports insufficient quota

Check the `OpenAI.GlobalStandard.gpt-5.6-terra` quota in Sweden Central. The
template requests capacity 100.

## AKS deployment is forbidden

```bash
kubectl auth can-i create deployments.apps --all-namespaces
```

The deploying user needs Azure Kubernetes Service RBAC Cluster Admin. Bicep
assigns it, but propagation takes a few minutes.

## The certificate stays pending

```bash
kubectl describe certificate order-agent-tls -n orders
kubectl logs -n cert-manager deployment/cert-manager
```

## Chat works but tools fail

```bash
kubectl logs -n orders deployment/order-agent-chat
kubectl logs -n orders deployment/fastmcp-orders
```

Check both service accounts carry the expected Workload Identity annotations.
The chat pod and the MCP pod have separate identities, and a missing annotation
on either produces the same symptom from the browser.

## `azd provision --preview` does not list the Databricks workspace

Expected. ARM what-if has no coverage for that provider, so it omits those
resources silently rather than reporting them unchanged. Use
`az bicep build --file infra/main.bicep --stdout` to confirm them.

## `azd deploy` leaves the kustomization files modified

Also expected. `azd deploy` writes an `images:` block into both
`k8s/*/kustomization.yaml` files naming the tag it just built. Those edits are
local to your deployment and are not meant to be committed.

## The hooks do nothing on Windows

Every hook in `azure.yaml` is declared twice, once under `posix` and once under
`windows`. If you added one and only wrote the `posix` block, azd will run
nothing on Windows and say nothing about it -- the ingress controller and the
Unity Catalog objects would simply be missing. See
[AGENTS.md](../AGENTS.md#adding-or-changing-an-azd-hook).

## Databricks mode

### The first query after a quiet period is slow, or asks for a retry

The warehouse stops after ten minutes idle and takes minutes to come back,
longer for the classic PRO fallback, which provisions a real VM. The tool polls
for 90 seconds, then returns a message saying the warehouse is starting and to
retry shortly, worded so it cannot be mistaken for an authorization decision.
The request that ran out of budget is the one that started the warehouse, so the
next attempt usually lands.

### `Unauthorized access to Org`

A valid Entra token is not enough on its own. The identity must also exist
inside the workspace, and a new workspace only knows whoever created it. The
bootstrap works around this by sending an ARM management token alongside the
Databricks token, so anyone holding Owner or Contributor on the workspace
resource is treated as a workspace admin and provisioned on first call.

The same requirement applies to end users: someone who has never signed in to
Databricks cannot be resolved by the MCP tools either. Provision them through
SCIM from Entra, or have them open the workspace once.

### `PERMISSION_DENIED ... CREATE FUNCTION` on a second run

Unity Catalog privileges are independent of workspace admin. A schema created by
one operator cannot be rebuilt by another until ownership moves, so the
bootstrap issues `ALTER SCHEMA ... OWNER TO` before touching anything inside it.

### `INVALID_STATE: Metastore storage root URL does not exist`

Expected on a workspace whose metastore was auto-provisioned. It has no storage
root, so `CREATE CATALOG` needs an explicit location. The bootstrap falls back
to the managed catalog Azure created with the workspace and reports the name it
used. Nothing to fix.
