# Troubleshooting a deployment

Failures specific to databricks mode are in
[databricks mode](databricks-mode.md). What this sample deliberately does not do
is in [limits](limits.md) -- check there before filing a bug, because several of
the behaviours people report are documented decisions.

## Bicep cannot create an app or permission grant

Activate an Entra application-management role, run `az login` again to pick it
up, and retry. Directory roles travel inside the access token, so activating one
does nothing until you sign in again.

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
