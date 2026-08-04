<!--
---
name: Delegated identity for MCP servers on Azure
description: An MCP server where every tool call runs as the person who asked, using the Entra on-behalf-of flow on AKS.
languages:
- python
- typescript
- bicep
- azdeveloper
products:
- azure
- azure-kubernetes-service
- azure-ai-foundry
- microsoft-entra-id
- azure-databricks
- azure-container-registry
page_type: sample
urlFragment: mcp-obo-accelerator
---
-->

# Delegated identity for MCP servers on Azure

An MCP server where every tool call runs as the person who asked — not as the
service. No secrets anywhere, and no application-permission path to user data.

Deployed with one `azd up` onto AKS, with a Microsoft Agent Framework chat app
in front of it.

[![Open in GitHub Codespaces](https://img.shields.io/static/v1?style=for-the-badge&label=GitHub+Codespaces&message=Open&color=brightgreen&logo=github)](https://codespaces.new/alisoliman/mcp-obo?devcontainer_path=.devcontainer%2Fdevcontainer.json)
[![Open in Dev Containers](https://img.shields.io/static/v1?style=for-the-badge&label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/alisoliman/mcp-obo)

## Important notice

This sample demonstrates a delegated identity pattern. It is not a hardened
deployment, and several things a production system needs are deliberately
absent — there is no human approval gate on tool calls, sessions live in one
pod's memory, and the Databricks workspace ships open to the network.
**[docs/limits.md](docs/limits.md) lists all of them.** Read it before adapting
this.

## Contents

- [Pick a mode](#pick-a-mode)
- [The one idea](#the-one-idea)
- [What runs where](#what-runs-where)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Get the code](#get-the-code)
  - [Deploy](#deploy)
- [Using it](#using-it)
- [Verify it](#verify-it)
- [Source layout](#source-layout)
- [Clean up](#clean-up)
- [Guidance](#guidance)

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

That is `src/mcp_server/obo.py`, shared by both. The request itself is a handful
of arguments; the rest of the file is the credential cache and the typed Entra
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

## Getting started

### Prerequisites

- Azure subscription Owner, or Contributor plus User Access Administrator.
- An Entra **Application Administrator**, Cloud Application Administrator,
  Privileged Role Administrator, or Global Administrator role, active. Directory
  roles travel inside the access token, so sign in again after activating one.
- At least 100K available Global Standard quota for `gpt-5.6-terra`.
- Azure CLI, Azure Developer CLI, `uv`, `kubectl`, `kubelogin`, and Helm 3.
- macOS, Linux, WSL, or Windows with PowerShell 7.

The [dev container](.devcontainer/devcontainer.json) installs every tool on that
last line, which is what the two badges above open. No local Docker daemon is
needed for a deployment — ACR builds both images remotely — and Node.js 22 is
needed only when changing the frontend.

### Get the code

```bash
azd init -t alisoliman/mcp-obo
```

This initializes a git repository, so there is no need to clone separately. Skip
it if you already have the repository checked out.

### Deploy

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

> These resources bill from the moment they exist, mostly for AKS and — in
> databricks mode — the workspace. `azd down` removes them.

Deploying again after an application change is `azd deploy`; after an `infra/`
or `azure.yaml` change it is `azd up`.

## Using it

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
uv run pytest
uv run ruff check . && uv run ruff format --check .
uvx mypy==2.3.0 --python-executable .venv/bin/python
az bicep build --file infra/main.bicep --stdout > /dev/null
npm --prefix frontend ci && npm --prefix frontend run build && npm --prefix frontend run lint
```

CI runs exactly these on every pull request, across Ubuntu and Windows.

Against a live deployment:

```bash
azd provision --preview --no-prompt
uv run pytest tests/e2e_databricks.py   # databricks mode only
```

The Python tests cover the order rules, per-user isolation, the mode switch, the
MCP token, scope and origin requirements, the tool annotations, the Databricks
host and identifier checks, the browser session and CSRF behaviour, that the
tool names the chat app allows are the ones the server registers, and that the
compiled template requests no application permission. They do not touch Azure,
so nothing here proves a deployment works — only that the parts that can be
decided locally are decided correctly. Everything else is checked by the tool
that owns it: Bicep for the template, `azd provision --preview` for the plan,
and the two image builds for the Dockerfiles.

## Source layout

```
src/mcp_server/   the protected resource        -> Dockerfile.mcp
src/chat_app/     the confidential client       -> Dockerfile.chat
src/shared/       what both must agree on
```

Two pods, two Entra app registrations, two service accounts. Each Dockerfile
copies only its own tree plus `shared`, so an import across that boundary fails
to build rather than shipping — and CI asserts that neither image can import the
other's entry module.

| Path | Purpose |
|---|---|
| `src/mcp_server/obo.py` | The on-behalf-of exchange. Both modes call it; only `scope` differs |
| `src/mcp_server/server.py` | Token verification, and the mode switch |
| `src/mcp_server/orders.py` | orders mode: Graph OBO, authorization in the application |
| `src/mcp_server/databricks.py` | databricks mode: Databricks OBO, authorization in Unity Catalog |
| `src/chat_app/` | Web login, safe session API, Agent Framework, delegated MCP calls |
| `src/shared/` | The mode switch and the tool names both workloads read |
| `scripts/bootstrap_databricks.py` | The Unity Catalog objects Bicep cannot create |
| `infra/main.bicep` | Azure, Foundry, Entra, consent, federation, and RBAC |
| `frontend/` | Vite React application with local shadcn UI |
| `k8s/` | Deployments, Services, issuer, and the shared Ingress |
| `tests/` | Unit tests, no deployment needed |
| `tests/e2e_databricks.py` | End-to-end proof against a live deployment |
| `docs/*.d2` | Diagram sources; the SVGs are committed alongside |

To follow the identity path in code, read the module docstring at the top of
`src/chat_app/app.py`, then `src/mcp_server/server.py`, then
`src/mcp_server/obo.py`. Between them they describe every token in the diagrams.

[AGENTS.md](AGENTS.md) has the full annotated tree and the procedures for
changes that span several files.

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

## Guidance

Full documentation is in the [docs](docs/README.md) folder:

- [How the authorization actually works](docs/authorization.md)
- [orders mode](docs/orders-mode.md)
- [databricks mode](docs/databricks-mode.md)
- [Local development](docs/localdev.md)
- [Troubleshooting](docs/troubleshooting.md)
- [What this sample deliberately does not do](docs/limits.md)
- [Contributing](CONTRIBUTING.md)

### References

- [Microsoft identity platform OAuth 2.0 On-Behalf-Of flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
- [Microsoft Agent Framework with Microsoft Foundry](https://learn.microsoft.com/agent-framework/agents/providers/microsoft-foundry)
- [Azure Developer CLI Helm and Kustomize support](https://learn.microsoft.com/azure/developer/azure-developer-cli/helm-kustomize-support)
- [Row filters and column masks in Unity Catalog](https://learn.microsoft.com/azure/databricks/data-governance/unity-catalog/filters-and-masks/)
