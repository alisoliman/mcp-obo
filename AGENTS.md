# Instructions for coding agents

Keep this file current. If a change moves a file or alters one of the
procedures below, update this file in the same commit — it is the map both
people and agents navigate by, and a stale map is worse than none.

- [Code layout](#code-layout)
- [Running the checks](#running-the-checks)
- [Adding an azd environment variable](#adding-an-azd-environment-variable)
- [Adding or changing an azd hook](#adding-or-changing-an-azd-hook)
- [Adding a tool](#adding-a-tool)
- [Adding a new mode](#adding-a-new-mode)
- [Changing what the agent is told](#changing-what-the-agent-is-told)
- [Upgrading dependencies](#upgrading-dependencies)
- [Where the claims are asserted](#where-the-claims-are-asserted)
- [Code style](#code-style)

## Code layout

Two workloads, deployed as two pods with two Entra app registrations and two
service accounts. The directory split is the trust boundary, not tidiness.

```
src/
  mcp_server/         the protected resource          -> Dockerfile.mcp  -> k8s/mcp
    server.py         token verification, the mode switch, the Origin check
    obo.py            the on-behalf-of exchange, its credential cache, its typed error
    orders.py         orders mode: Graph OBO, authorization decided in this process
    databricks.py     databricks mode: Databricks OBO, authorization decided by Unity Catalog
    __main__.py       `python -m src.mcp_server`, the container entry point
  chat_app/           the confidential client         -> Dockerfile.chat -> k8s/chat
    app.py            the HTTP surface: routes, the SSE turn, the app factory
    settings.py       everything read from the environment
    sessions.py       BrowserSession, the per-pod store, the cookie rules
    auth.py           MSAL sign-in, and the user's delegated token
    events.py         what the browser is allowed to see of a turn
    security.py       the response headers
    prompts.py        which tools and which instructions, per mode
    prompts/*.md      the instruction text itself
    __main__.py       `python -m src.chat_app`, the container entry point
  shared/             what both must agree on; no framework imports belong here
    modes.py          MCP_MODE and its resolution
    tools.py          the tool names, which are a contract between the two

infra/main.bicep      Azure, Foundry, Entra apps, consent, federation, RBAC
azure.yaml            the azd hooks and the two service definitions
k8s/{mcp,chat}/       Deployment, Service, ServiceAccount, kustomization
scripts/              operator entry points, run through `uv run python -m`
frontend/             Vite + React, built into the chat image by ACR
tests/                unit tests; `e2e_databricks.py` needs a deployment
docs/                 the topic docs the README links out to
```

The two workloads must not import each other. Each Dockerfile copies only its
own tree plus `src/shared`, and `.github/workflows/images.yml` asserts that
neither image can import the other's entry module. Anything both need goes in
`src/shared`.

The azd service names (`mcp`, `chat` in `azure.yaml`) map to `src/mcp_server`
and `src/chat_app`. They differ because `src/mcp` would shadow the installed
`mcp` package.

## Running the checks

```bash
uv run pytest                    # unit tests, no Azure
uv run ruff check .              # lint
uv run ruff format .             # format
uvx mypy==2.3.0 --python-executable .venv/bin/python   # types, src and scripts
```

`tests/e2e_databricks.py` deliberately does not match pytest's `test_*.py`
pattern, so a bare `pytest` never calls a deployed stack. Run it by name.

## Adding an azd environment variable

Four links, and every one is easy to miss. A variable that stops at link three
produces a pod that crashes on startup with `X is required`; one that stops at
link two produces an empty string, which is worse.

1. **`infra/main.bicep`** — add an `output NAME string = ...` at the bottom. azd
   writes every output into the environment after `provision`.
2. **`azure.yaml`** — add `NAME: ${NAME}` under the `kustomize.env` block of
   whichever service needs it. There are two service definitions; a variable
   both workloads read has to appear under both.
3. **`k8s/<service>/`** — nothing, usually. azd writes the `env` block to a
   `.env` file that `configMapGenerator` turns into a ConfigMap, and the
   Deployment already pulls the whole ConfigMap in with `envFrom`. You only
   touch the manifests if the value must become an annotation rather than an
   environment variable — see the `replacements` block in
   `k8s/mcp/kustomization.yaml`, which is how the Workload Identity client and
   tenant ids reach the ServiceAccount.
4. **`src/*/settings.py`** (or `DatabricksSettings.from_env`) — read it, and
   decide whether it is required. `required()` fails at startup with the name in
   the message; `os.getenv(name, "")` treats absent as empty. Prefer failing.

If the variable is one an operator sets rather than one Bicep produces, also add
it to `infra/main.parameters.json` and document it — `ALLOWED_PRINCIPAL_IDS` and
`ENABLE_DATABRICKS_AUDIT_LOGS` are the existing examples, both described in
`docs/limits.md`.

## Adding or changing an azd hook

Every hook in `azure.yaml` is declared twice, once under `posix` and once under
`windows`. **azd runs only the block matching the host, and says nothing when
there is no match.** A hook with only a `posix` block silently does nothing on
Windows — which for `predeploy` means deploying onto a cluster with no ingress
controller and no cert-manager, and for `postprovision` means a databricks-mode
deployment with no Unity Catalog objects.

So: change one block, change the other. The differences that matter between
them:

- PowerShell does not treat `\` as an escape inside a double-quoted string, so
  Helm's dotted annotation keys are written literally there and quoted a second
  time in `sh`.
- Native command failures do not stop a pwsh script by default. Both Windows
  blocks set `$ErrorActionPreference` and
  `$PSNativeCommandUseErrorActionPreference` at the top.
- Line continuation is `` ` `` in PowerShell and `\` in sh.

Scripts under `scripts/` need no shell-specific variant: they are Python, run
through `uv run python -m`, which works identically on both.

## Adding a tool

1. Write it in `src/mcp_server/orders.py` or `src/mcp_server/databricks.py`,
   inside that module's `register()`.
2. Take the caller's token through the `AccessToken` dependency and resolve the
   user from it. Every tool does; that is what makes per-user isolation
   structural rather than remembered.
3. **Annotate it.** An unannotated tool reads as writing, and writing reads as
   destructive, to anything applying a policy of its own. `_READ_ONLY`,
   `_ADDITIVE` and `_ONE_WAY` in `orders.py` are the three shapes in use.
4. Add the name to `src/shared/tools.py`. `tests/test_contracts.py` asserts that
   the registered set and the declared set are equal, so skipping this fails the
   build rather than quietly removing the tool from the agent's allow-list.
5. Raise `ToolError` for anything a caller could act on. FastMCP runs with
   `mask_error_details=True`, which flattens every other exception into one
   indistinguishable string — right for a bug, wrong for a Conditional Access
   policy or a missing consent.

## Adding a new mode

The `register(mcp, exchange, ...)` function is the seam. A mode is: one module
under `src/mcp_server/` that exposes it, one scope constant in `obo.py`, one
entry in `src/shared/modes.py`, one tool-name set in `src/shared/tools.py`, one
prompt in `src/chat_app/prompts/`, and one branch in `create_server()`.

1. `src/shared/modes.py` — add the constant and put it in `MODES`.
2. `src/mcp_server/obo.py` — add the downstream scope. If the resource is a
   first-party Azure service, its application id is a fixed constant across
   tenants; say so in a comment the way `DATABRICKS_RESOURCE_APP_ID` does.
3. `src/mcp_server/<mode>.py` — the tools, and a `register()` that takes the
   `OboExchange`.
4. `src/mcp_server/server.py` — add the branch in `create_server()` and the
   display names in `_MODE_NAMES`.
5. `src/shared/tools.py` and `src/chat_app/prompts/<mode>.md` — the tool names
   and the instructions. `src/chat_app/prompts.py` selects both by mode; extend
   the two conditionals there.
6. `infra/main.bicep` — the delegated permission for the new resource, inside
   the mode-dependent `requiredResourceAccess` list. It must be `type: 'Scope'`.
   `tests/test_infrastructure.py` compiles the template and fails on
   `type: 'Role'`, and also asserts the number of permissions, so that count
   needs updating alongside.
7. `tests/test_contracts.py` — add the mode.
8. `README.md` and `docs/README.md` — the mode table, and a `docs/<mode>-mode.md`.

## Changing what the agent is told

`src/chat_app/prompts/orders.md` and `src/chat_app/prompts/databricks.md`. They
are read at first use and cached, so a change needs a restart, not a rebuild of
anything else. Nothing else in the repo holds instruction text.

## Upgrading dependencies

Python:

```bash
uv lock --upgrade          # or: uv lock --upgrade-package fastmcp
uv sync
uv run pytest
```

`fastmcp` is pinned exactly (`==`) rather than floored. It is the one dependency
whose internals this repo reaches into — `SameOriginOnly` exists because
`TransportSecurityMiddleware` is constructed with no settings in 3.2.4, and
`src/mcp_server/orders.py` explains why the `EntraOBOToken` dependency is not
used. Read the changelog for both before moving the pin, and re-check those two
comments against the new version.

Frontend:

```bash
npm --prefix frontend update
npm --prefix frontend run build
npm --prefix frontend run lint
```

Dependabot opens grouped pull requests weekly for both, and monthly for the
GitHub Actions and the base images.

Pinned versions living outside the lockfiles, all in `azure.yaml`: the
ingress-nginx chart, the cert-manager chart. The base image tags are in the two
Dockerfiles, and the Node version is in `frontend/.nvmrc`.

## Where the claims are asserted

This repo makes claims in prose that are checked in code. When you change
something a claim depends on, the test is where you will find out.

| Claim | Asserted by |
|---|---|
| No application permission is ever requested, in either mode | `tests/test_infrastructure.py`, against the compiled ARM template |
| The agent's allow-list is the set the server registers | `tests/test_contracts.py` |
| The two workloads ship separately | `.github/workflows/images.yml` |
| A token minted for another resource is refused | `tests/test_mcp_server.py` |
| One caller's credential is never served to another | `tests/test_obo.py` |
| Unity Catalog, not this code, filters the rows | `tests/e2e_databricks.py`, needs a deployment |

## Code style

`ruff` decides formatting and import order.

Comments explain why the code is the shape it is — which constraint, which
provider behaviour, which failure it avoids. They do not restate the next line.
If a comment could be deleted without losing a fact that is absent from the
code, delete it.

Prefer failing at startup over degrading quietly. `_required_env`,
`_validated_host` and the frontend-build check in `create_app` all refuse to
start rather than run in a shape nobody intended.
