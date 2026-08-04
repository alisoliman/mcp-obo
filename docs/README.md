# Documentation

The [main README](../README.md) has the deployment path. These are the topics
that would have made it too long to read.

**Start here**

- [How the authorization actually works](authorization.md) — the three
  identities, the three gates, and why Microsoft Graph is not the authorization
  system. Read it once and the rest of the repo follows.

**The two modes**

You deploy one. They are alternatives, not layers.

- [orders mode](orders-mode.md) — the MCP server authorizes, using Microsoft
  Graph to say who you are
- [databricks mode](databricks-mode.md) — Unity Catalog authorizes, with row
  filters and column masks applied before the server sees a row

**Deploying and running**

- [Local development](localdev.md) — what runs on your machine, and what
  genuinely needs a deployment first
- [Troubleshooting](troubleshooting.md) — the failures that come up most often
- [What this sample deliberately does not do](limits.md) — read before adapting
  it

**Contributing**

- [Contributing](../CONTRIBUTING.md) — setup, the checks, and code style
- [AGENTS.md](../AGENTS.md) — the annotated file tree, and the procedures for
  changes that span several files

**Diagrams**

`architecture`, `token-flow`, `trust-boundaries` and `databricks-obo` are
committed as both `.d2` source and rendered `.svg`. See
[local development](localdev.md#rebuilding-the-diagrams) for how to re-render
them.
