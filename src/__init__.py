"""Two workloads and the contract between them.

    src/mcp_server/  the FastMCP resource server        -> Dockerfile.mcp
    src/chat_app/    the confidential client in front   -> Dockerfile.chat
    src/shared/      what both must agree on

The split is the point rather than tidiness. These are separate pods with
separate Entra app registrations and separate service accounts, and the whole
claim of this accelerator is that neither can reach user data without the other.
A directory each keeps that boundary visible, and each Dockerfile copies only
its own tree plus `shared`, so an import across the boundary fails to build
rather than shipping.
"""
