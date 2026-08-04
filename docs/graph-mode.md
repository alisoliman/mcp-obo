# Graph mode

**The persona:** you want to build your own MCP server on Azure, with real
delegated identity, and your service owns its own data.

The store here is an in-memory dict. Everything around it is production-shaped:
two app registrations, two federated credentials, no secrets anywhere, and an
on-behalf-of exchange behind every call.

## What it deploys

Only the base stack — no Databricks resources, and no Databricks permission on
the server application:

- One-node Free-tier AKS with Entra integration, Azure RBAC, OIDC and Workload
  Identity, plus Basic ACR, a static public IP, ingress-nginx and cert-manager.
- A Microsoft Foundry resource, project, and a `gpt-5.6-terra` deployment.
- Two Entra app registrations, two federated credentials, and tenant-wide
  consent for delegated `Mcp.Tools.ReadWrite` and Graph `User.Read`.

## The tools

| Tool | Behaviour |
|---|---|
| `who_am_i` | The safe Graph `/me` identity proof resolved through OBO |
| `list_orders` | Orders owned by the signed-in user |
| `get_order` | One owned order |
| `create_order` | A validated order |
| `update_order_status` | A valid status transition |

Orders move `pending → processing → shipped → delivered`. Pending or processing
orders can be cancelled. Repeating the current status is idempotent.

The three read tools are annotated `readOnlyHint`, `create_order` as writing but
not destructive, and `update_order_status` as destructive and idempotent, since
one of its transitions is cancellation. Those annotations are hints for a client
to act on, and the chat app in this repo does not: it auto-approves every tool
call, because the one-way SSE stream has nowhere to ask. So `create_order` and
`update_order_status` run on arguments a model chose after reading text this
application does not control. Build an approval channel before exposing a
mutating tool that way.

The identity proof returns the Graph display name, an eight-character hash of
the object id, and the evidence label `Microsoft Graph /me via OBO`. It never
returns the raw object id, the token, or any claim.

## Demonstrating isolation

Keep the single MCP pod running throughout, because the store is in memory.

1. Sign in as user A and create an order.
2. Ask the agent to prove the MCP identity. Note A's display name and
   fingerprint from the `Who Am I` tool card.
3. Open an InPrivate window and sign in as user B.
4. List orders. A's order is absent.
5. Ask for A's order id directly. The answer is `Order not found`, which does
   not disclose whether someone else owns that id.
6. Ask for the identity proof again. B has a different name and fingerprint.

## Where the authorization happens

In `src/mcp_server/orders.py`, and it is worth being honest about this:

```python
user = await current_user(access_token)  # Graph /me, over OBO
return list_orders_for(user.id)  # <-- the authorization decision
```

That second line is the entire access control. It is correct only for as long
as every tool remembers to pass the caller's id and nothing else. For a service
that owns its data, that is a perfectly good trade — it is the model most MCP
servers need.

[Databricks mode](databricks-mode.md) is the same identity path with that
decision moved out of the process, so the server cannot get it wrong.

## Adapting it to your own tools

1. Replace the tool functions in `src/mcp_server/orders.py`. Keep the
   `access_token: AccessToken = CurrentAccessToken()` parameter and the
   `current_user(access_token)` call at the top of each one. That pair is the
   whole mechanism by which a tool becomes user-scoped. An Entra refusal inside
   that call reaches the user as a `ToolError` naming the AADSTS code; the Graph
   request after it does not, so a Graph 403 is still flattened into an
   indistinguishable error by `mask_error_details`.
2. Swap the in-memory dict for a real store, partitioned by the Graph object id
   rather than by anything the model passed in.
3. Rename `scopeValue` in `infra/main.bicep` if you want a different scope, and
   add further downstream permissions to `serverApp.requiredResourceAccess`
   alongside a matching `oauth2PermissionGrants` resource.
4. Rewrite `src/chat_app/prompts/graph.md` for your domain.

The identity plumbing does not change. The mode name does, in more places than
this file: a scope constant, a module, a tool-name set, a prompt, the Bicep
`@allowed` list, the frontend and the issue template. The checklist is
[Adding a new mode](../AGENTS.md#adding-a-new-mode) in `AGENTS.md`.
