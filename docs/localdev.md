# Local development

Be clear about what this loop can and cannot give you.

The claim this sample makes -- that a tool call runs as the person who asked --
rests on AKS Workload Identity federation, which is a signed token a Kubernetes
cluster projects into a pod. Nothing on your laptop projects that token, so the
on-behalf-of exchange cannot happen locally. Anything downstream of it (Graph
`/me`, Unity Catalog row filters, the Databricks audit row naming a human)
requires a deployment.

What you can do locally is everything else, which is most of the code.

## The fast loop: tests

```bash
uv sync
uv run pytest
```

No Azure, no deployment, no secrets, about six seconds. This covers the order
rules and per-user isolation, the mode switch, the MCP token, scope and origin
requirements, the tool annotations, the Databricks host and identifier checks,
the browser session and CSRF behaviour, the polling and retry loops driven
against a scripted transport, that the tool names the chat app allows are the
ones the server registers, and that the compiled Bicep template requests no
application permission.

What it does not cover is whether a deployment works. Nothing here talks to
Azure. Everything else is checked by the tool that owns it: Bicep for the
template, `azd provision --preview` for the plan, and the two image builds for
the Dockerfiles.

`tests/e2e_databricks.py` is the module that does talk to a live deployment. It
does not match pytest's `test_*.py` collection pattern, so a bare `pytest` never
picks it up. Run it by name, after `azd up`:

```bash
uv run pytest tests/e2e_databricks.py
```

## The frontend, against a deployed backend

The frontend is a Vite application and its dev server has hot reload. Point it
at a deployment and you get the real API without rebuilding an image per change:

```bash
CHAT_BACKEND=$(azd env get-value APP_URL) npm --prefix frontend run dev
```

`frontend/vite.config.ts` reads `CHAT_BACKEND` and proxies `/api`, `/login`,
`/logout`, `/auth` and `/health` to it. Without the variable the dev server
serves the bundle and nothing else, which is what you want when working on a
component in isolation.

Two caveats, both about the session cookie. It is `__Host-` prefixed, which
requires `Secure`, so a plain `http://localhost` origin will not keep it -- run
the dev server over HTTPS (`--https` with a local certificate) or accept that
you are testing the signed-out view. And `OAUTH_REDIRECT_URI` points at the
deployed host, so the sign-in round trip lands there rather than back on your
dev server.

## The chat app, running on your machine

This works for everything except the MCP call itself.

```bash
npm --prefix frontend run build     # the app serves the built bundle

export AZURE_TENANT_ID=...
export ENTRA_CLIENT_CLIENT_ID=...          # the client app registration
export OAUTH_REDIRECT_URI=https://localhost:8001/auth/callback
export APP_URL=https://localhost:8001
export MCP_SCOPE=api://<server-app-id>/Mcp.Tools.ReadWrite
export MCP_ENDPOINT=https://<deployed-host>/mcp
export FOUNDRY_PROJECT_ENDPOINT=...
export FOUNDRY_MODEL_DEPLOYMENT_NAME=...
export AZURE_FEDERATED_TOKEN_FILE=/dev/null

uv run python -m src.chat_app
```

`azd env get-values` prints all of these for a deployment you already have.

Add `https://localhost:8001/auth/callback` as a redirect URI on the client app
registration first, or Entra will refuse the sign-in.

What will fail: `WorkloadIdentityCredential` has no projected token to read, so
the Foundry call and the client assertion both fail. That makes this useful for
working on routing, session handling, CSRF, the security headers and the static
serving -- not for working on a chat turn. For a turn, deploy.

## The MCP server, running on your machine

```bash
export AZURE_TENANT_ID=...
export ENTRA_SERVER_CLIENT_ID=...
export MCP_SERVER_URL=http://localhost:8000
export MCP_MODE=orders
export AZURE_FEDERATED_TOKEN_FILE=/dev/null

uv run python -m src.mcp_server
```

It will start, serve its RFC 9728 discovery documents, and reject every tool
call: `AzureJWTVerifier` still validates real tokens against your tenant, and the
on-behalf-of exchange still needs a client assertion it cannot produce. That is
enough to work on the discovery documents, the `Origin` check and the tool
schemas.

## Rebuilding the diagrams

The SVGs are committed alongside their `.d2` sources. After editing a `.d2`:

```bash
for name in architecture token-flow trust-boundaries databricks-obo; do
  d2 "docs/$name.d2" "docs/$name.svg"
done
```

## Deploying a code change

If you changed only application code, you do not need to re-provision:

```bash
azd deploy
```

If you changed `infra/` or `azure.yaml`, you do:

```bash
azd up
```
