"""End-to-end proof that the delegated identity reaches Unity Catalog.

Everything here runs against the deployed stack: it signs in as a real user,
calls the real MCP endpoint over HTTPS, and asserts on what Databricks sent
back. Nothing is mocked, because the claim being tested -- that Unity Catalog,
not this codebase, decides which rows come back -- cannot be tested any other
way.

Run it after `azd up`:

    uv run pytest tests/e2e_databricks.py

The filename is the guard. pytest collects `test_*.py`, so a bare `pytest` never
picks this module up and the fast local loop cannot quietly become live round
trips once someone has deployed. Naming the file is the only way to run it. The
module also skips itself when the environment is not configured.

A few checks read workspace configuration -- a warehouse ACL, the query history
-- instead of calling the MCP server, because what they assert is a grant rather
than a result. Those need a Databricks privilege the person running the harness
may not hold, so each one is guarded on its own and skips with the reason. A
harness that fails on a laptop teaches people to stop reading it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import unittest
from urllib.parse import urlsplit

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from src.mcp_server.obo import DATABRICKS_RESOURCE_APP_ID


def azd_env() -> dict[str, str]:
    """Read the azd environment, falling back to the process environment."""
    values = dict(os.environ)
    try:
        output = subprocess.run(
            ["azd", "env", "get-values", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        values.update(json.loads(output))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        pass
    return values


ENV = azd_env()
MCP_ENDPOINT = ENV.get("MCP_ENDPOINT", "").strip().rstrip("/")
SERVER_APP_ID = ENV.get("ENTRA_SERVER_CLIENT_ID", "").strip()
DATABRICKS_HOST = ENV.get("DATABRICKS_HOST", "").strip().rstrip("/")
TENANT_ID = ENV.get("AZURE_TENANT_ID", "").strip()
WAREHOUSE_ID = ENV.get("DATABRICKS_WAREHOUSE_ID", "").strip()
# The bootstrap resolves each owner through SCIM and lowercases it before
# seeding, so this matches how they are stored rather than how they were typed.
DEMO_OWNERS = [
    owner.strip().lower()
    for owner in ENV.get("DATABRICKS_DEMO_OWNERS", "").split(",")
    if owner.strip()
]
# A graph-mode deployment serves none of these tools, so the whole module
# skips rather than failing on tools that were never meant to be there.
MODE = ENV.get("MCP_MODE", "graph").strip().lower() or "graph"
CONFIGURED = bool(
    MODE == "databricks" and MCP_ENDPOINT and SERVER_APP_ID and DATABRICKS_HOST
)

# Written back by the bootstrap, so these follow however many owners were seeded.
SEEDED_TOTAL = int(ENV.get("DATABRICKS_SEEDED_TOTAL", "0") or 0)
SEEDED_FOR_USER = int(ENV.get("DATABRICKS_SEEDED_PER_OWNER", "3") or 3)
SEEDED_FOR_USER_IN_EU = 2

# The permissions on a SQL warehouse that let the holder run a statement.
# CAN_MONITOR and CAN_VIEW are deliberately absent: they show someone the query
# history and stop there, which is precisely the near-miss this checks for.
RUN_QUERY_PERMISSIONS = frozenset({"CAN_USE", "CAN_MANAGE", "IS_OWNER"})

# Query history is written asynchronously, so the row for a statement that just
# finished may not be there on the first look.
HISTORY_ATTEMPTS = 4
HISTORY_DELAY = 2.0

_RESOURCE_METADATA = re.compile(r'resource_metadata="?([^",\s]+)"?')


def run(command: list[str]) -> str:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=180, check=True
    ).stdout.strip()


def run_graph(test: unittest.TestCase, command: list[str]) -> str:
    """Run an `az` command that reaches Microsoft Graph.

    Activating a PIM role changes conditional-access policy, and Graph then
    rejects tokens minted before the change. That is a stale local login, not a
    defect in the deployment, so it skips with instructions. Every other error
    still fails.
    """
    try:
        return run(command)
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "") + (error.stdout or "")
        if "InteractionRequired" in stderr or "AADSTS" in stderr:
            test.skipTest(
                "the Azure CLI's Microsoft Graph token needs refreshing; "
                "run `az login` and try again"
            )
        raise


def user_token() -> str:
    """A delegated token for the MCP server, minted for the signed-in user.

    This works only because `harnessClientAppId` pre-authorized the Azure CLI on
    the server application -- opt-in and off by default, see infra/main.bicep.
    Nothing in the running system depends on it. Cached for the run: the token
    lasts an hour and shelling out to `az` costs about a second.
    """
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                f"api://{SERVER_APP_ID}",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ]
        )
    return _TOKEN


_TOKEN: str | None = None


def signed_in_upn() -> str:
    """The signed-in user, read from the local CLI profile.

    Deliberately not `az ad signed-in-user show`: that reaches Microsoft Graph,
    which would make the identity assertions depend on a second service having a
    healthy token.
    """
    return run(["az", "account", "show", "--query", "user.name", "-o", "tsv"])


def workspace_token() -> str:
    """A Databricks token for the signed-in user, minted without the OBO path.

    The checks that use this read workspace configuration -- a warehouse ACL,
    the query history -- which the MCP server does not expose and should not.
    Azure Databricks pre-authorizes the Azure CLI on its own resource
    application, so unlike `user_token` this needs nothing added to the
    deployment.
    """
    global _WORKSPACE_TOKEN
    if _WORKSPACE_TOKEN is None:
        _WORKSPACE_TOKEN = run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                DATABRICKS_RESOURCE_APP_ID,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ]
        )
    return _WORKSPACE_TOKEN


_WORKSPACE_TOKEN: str | None = None


def workspace_get(
    test: unittest.TestCase,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
    allow: tuple[int, ...] = (),
) -> dict | None:
    """Read a Databricks workspace API as the signed-in user, or skip.

    Nothing here is part of the delegated path, so a refusal says something
    about the person running the harness rather than about the deployment: 401
    means they have never been provisioned in this workspace, 403 that they hold
    no admin right over the object. Neither is a regression, so both skip with
    the reason. `allow` returns None for a status the caller has another answer
    for, the same shape `scripts/bootstrap_databricks.py` uses.
    """
    try:
        token = workspace_token()
    except subprocess.CalledProcessError:
        test.skipTest("the Azure CLI could not mint a Databricks token; run `az login`")
    response = httpx.request(
        "GET",
        f"{DATABRICKS_HOST}{path}",
        params=params,
        json=json_body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if response.status_code in (401, 403):
        test.skipTest(
            f"Databricks returned {response.status_code} for {path}: this check "
            "reads workspace configuration and needs a privilege the account "
            "running the harness does not have"
        )
    if response.status_code in allow:
        return None
    response.raise_for_status()
    return response.json()


def query_history(test: unittest.TestCase, statement_id: str) -> dict | None:
    """The workspace's own record of one statement, or None if it has not landed.

    Query history is written asynchronously, so a row that is missing seconds
    after the statement finished is not a defect. That is also the limit of what
    this can prove: it can confirm who a statement was credited to, never that
    nothing else ran.
    """
    path = "/api/2.0/sql/history/queries"
    # `filter_by` is a structured field on a GET, which Databricks takes as a
    # JSON body. Where that is refused -- and which status says so depends on
    # what sits in front of the workspace -- the recent listing is scanned
    # instead. That is enough here because the statement ran seconds ago and the
    # listing comes back newest first.
    filtered = {"filter_by": {"statement_ids": [statement_id]}, "max_results": 10}
    refused = (400, 404, 405, 415)
    for attempt in range(HISTORY_ATTEMPTS):
        if attempt:
            time.sleep(HISTORY_DELAY * attempt)
        body = workspace_get(test, path, json_body=filtered, allow=refused)
        if body is None:
            body = workspace_get(test, path, params={"max_results": 100}) or {}
        for entry in body.get("res") or []:
            # Databricks names this `query_id` in query history and
            # `statement_id` in the Statement Execution API. They are the same
            # value, and which key carries it has moved between versions.
            ids = {
                str(entry.get("query_id") or ""),
                str(entry.get("statement_id") or ""),
            }
            if statement_id in ids:
                return entry
    return None


def metadata_url(endpoint: str) -> str:
    """Where RFC 9728 section 3.1 says this resource's metadata lives.

    Computed rather than read from the environment, because this string is the
    one the ingress has to route: `k8s/chat/ingress.yaml` matches it with an
    `Exact` rule, and every other path on the host goes to the chat app.
    """
    parts = urlsplit(endpoint)
    return (
        f"{parts.scheme}://{parts.netloc}"
        f"/.well-known/oauth-protected-resource{parts.path}"
    )


async def call(tool: str, arguments: dict | None = None):
    transport = StreamableHttpTransport(
        url=MCP_ENDPOINT, headers={"Authorization": f"Bearer {user_token()}"}
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments or {})

    # `result.data` is a generated model rather than a mapping. FastMCP wraps a
    # non-object return value -- a list of tables, say -- in a `result` key.
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class DelegatedIdentityReachesDatabricks(unittest.TestCase):
    """The identity survives every hop between the browser and the warehouse.

    Every assertion here reads what the deployment actually returned, which is
    the only way this claim can be tested -- except the last, which asks the
    workspace who it credited the statement to. That one reads query history and
    so needs a privilege the harness may not hold, so it skips on its own rather
    than taking the rest with it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.upn = signed_in_upn()
        # One query, shared by the assertions that read different columns of it.
        # Re-running it per test would mean another MCP session, another OBO
        # exchange and another warehouse statement for the same answer.
        cls.orders = asyncio.run(call("databricks_query_orders"))

    def test_databricks_resolves_the_signed_in_user_not_the_server(self) -> None:
        identity = asyncio.run(call("databricks_whoami"))

        # SCIM answers from the control plane, current_user() from the SQL
        # warehouse. Both naming the same human is the proof that the delegated
        # token survived the OBO exchange and both hops beyond it.
        self.assertEqual(identity["scim_user_name"].lower(), self.upn.lower())
        self.assertEqual(identity["sql_current_user"].lower(), self.upn.lower())
        self.assertEqual(
            identity["evidence"], "Databricks SCIM /Me and SQL current_user() via OBO"
        )

    def test_unity_catalog_filters_rows_before_the_server_sees_them(self) -> None:
        result = self.orders

        # The table holds SEEDED_TOTAL rows. The query has no predicate on
        # owner_email. The server does no filtering. Fewer rows come back
        # because Unity Catalog applied the row filter to the caller's identity.
        self.assertEqual(result["row_count"], SEEDED_FOR_USER)
        self.assertLess(SEEDED_FOR_USER, SEEDED_TOTAL)
        self.assertEqual(result["ran_as"].lower(), self.upn.lower())

        owner = result["columns"].index("owner_email")
        for row in result["rows"]:
            self.assertEqual(row[owner].lower(), self.upn.lower())

    def test_the_column_mask_hides_customer_email(self) -> None:
        result = self.orders
        masked = result["columns"].index("customer_email")

        for row in result["rows"]:
            self.assertTrue(
                row[masked].startswith("***@"),
                f"expected a masked address, got {row[masked]!r}",
            )

    def test_a_bound_parameter_narrows_the_result(self) -> None:
        result = asyncio.run(call("databricks_query_orders", {"region": "EU"}))

        self.assertEqual(result["row_count"], SEEDED_FOR_USER_IN_EU)
        region = result["columns"].index("region")
        for row in result["rows"]:
            self.assertEqual(row[region], "EU")

    def test_the_demo_schema_is_visible(self) -> None:
        listing = asyncio.run(call("databricks_list_tables"))
        names = {table["full_name"].split(".")[-1] for table in listing["tables"]}
        self.assertIn("orders", names)

        # The demo schema holds one table, nowhere near a page boundary, so
        # `more_available` being true here means the tool stopped paging early.
        # That matters more than it looks: Databricks documents that a page may
        # come back empty while still carrying a next-page token, so a listing
        # that stops on the first empty page is indistinguishable from Unity
        # Catalog denying everything -- the one distinction this accelerator
        # exists to make legible.
        self.assertFalse(listing["more_available"])

    def test_the_result_is_complete_and_names_its_statement(self) -> None:
        result = self.orders

        # The seeded rows for one owner sit far below the default limit, so
        # nothing here was trimmed. Asserted rather than assumed because
        # widening the query is exactly what docs/databricks-mode.md invites,
        # and a result the warehouse cut short is otherwise indistinguishable
        # from a complete one to whoever summarises it for the user.
        self.assertFalse(result["truncated"])

        # The statement id is what joins this answer to the workspace's own
        # audit record of it, which the query history check below follows.
        self.assertTrue(result["statement_id"], "the server reported no statement id")

    def test_the_workspace_credits_the_statement_to_the_human(self) -> None:
        entry = query_history(self, self.orders["statement_id"])
        if entry is None:
            self.skipTest(
                "the query history row for this statement has not appeared; it "
                "is written asynchronously, so its absence proves nothing"
            )

        # The claim the whole accelerator rests on, read back from the system of
        # record rather than from the server's own answer: Databricks logged the
        # signed-in person, not the workload identity that relayed the question.
        # The statements also carry a distinctive User-Agent, which is what lets
        # an operator pick them out of the same history by eye; nothing is
        # asserted about it because Databricks does not document surfacing it,
        # and attribution is the property that must hold either way.
        credited = {
            field: str(entry[field]).lower()
            for field in ("user_name", "executed_as_user_name")
            if entry.get(field)
        }
        self.assertTrue(credited, f"the query history row names nobody: {entry}")
        for field, name in credited.items():
            self.assertEqual(name, self.upn.lower(), f"query history {field}")


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class NoApplicationOnlyPathExists(unittest.TestCase):
    """The negative half of the claim, checked against live Entra ID.

    A metastore admin bypasses most Unity Catalog grants, so "the admin cannot
    read table X" would be a weak test. "The server holds no permission that
    works without a user" is the property the accelerator actually rests on, and
    it holds no matter who runs these.
    """

    def test_the_server_holds_no_application_permission(self) -> None:
        permissions = json.loads(
            run_graph(
                self,
                [
                    "az",
                    "ad",
                    "app",
                    "permission",
                    "list",
                    "--id",
                    SERVER_APP_ID,
                    "-o",
                    "json",
                ],
            )
        )
        granted = [
            access
            for permission in permissions
            for access in permission.get("resourceAccess", [])
        ]
        self.assertTrue(granted, "expected at least the delegated permissions")
        for access in granted:
            # 'Role' is an application permission: usable with no user present.
            # 'Scope' is delegated: meaningless without one.
            self.assertEqual(
                access["type"], "Scope", f"unexpected app permission: {access}"
            )

    def test_the_server_principal_has_no_app_role_assignments(self) -> None:
        principal = json.loads(
            run_graph(
                self, ["az", "ad", "sp", "show", "--id", SERVER_APP_ID, "-o", "json"]
            )
        )
        assignments = json.loads(
            run_graph(
                self,
                [
                    "az",
                    "rest",
                    "--method",
                    "GET",
                    "--url",
                    f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal['id']}"
                    "/appRoleAssignments",
                    "-o",
                    "json",
                ],
            )
        )
        self.assertEqual(
            assignments.get("value", []),
            [],
            "the MCP server has been granted an application permission",
        )


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class TheChallengeTellsAClientWhereToGetAToken(unittest.TestCase):
    """RFC 9728 discovery, read over the ingress that actually serves it.

    A 401 alone leaves a client that holds no token with nowhere to go. The MCP
    authorization spec's answer is the `resource_metadata` parameter on the
    challenge and the document it names, and three things have to agree for that
    to work here: `MCP_SERVER_URL`, the path FastMCP mounts the server at, and
    the ingress rule that routes `/.well-known/oauth-protected-resource/mcp` to
    the MCP service instead of to the chat app that owns `/`. They agree today
    by convention. This is what binds them, and it is the ingress rule that goes
    missing most often, because a resource server sharing an origin with a web
    app is the case nobody remembers to route.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.response = httpx.post(
            MCP_ENDPOINT,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=30,
        )

    def advertised(self) -> str:
        challenge = self.response.headers.get("www-authenticate", "")
        self.assertTrue(
            challenge.lower().startswith("bearer"),
            f"expected a Bearer challenge, got {challenge!r}",
        )
        found = _RESOURCE_METADATA.search(challenge)
        self.assertIsNotNone(
            found, f"the challenge names no metadata document: {challenge!r}"
        )
        return found.group(1)

    def test_an_unauthenticated_call_is_challenged_with_its_metadata_document(
        self,
    ) -> None:
        self.assertEqual(self.response.status_code, 401)

        # Compared against the RFC 9728 construction rather than merely asserted
        # to exist: this exact string is what the ingress has to match, so a
        # server mounted at a different path or published under a different
        # hostname is caught here rather than by a client that cannot sign in.
        self.assertEqual(self.advertised(), metadata_url(MCP_ENDPOINT))

    def test_the_advertised_document_describes_this_server(self) -> None:
        document = httpx.get(self.advertised(), timeout=30)

        # Content, not just status. Every other path on this hostname belongs to
        # the chat app, so a missing ingress rule does not produce a 404 -- it
        # produces the single-page app's own HTML with a cheerful 200.
        self.assertEqual(document.status_code, 200, document.text[:200])
        self.assertIn(
            "json",
            document.headers.get("content-type", ""),
            "the well-known path is not being routed to the MCP server",
        )
        metadata = document.json()

        # A client uses `resource` as the audience it asks Entra for. Naming
        # anything but the endpoint it just called yields a token this server
        # will refuse.
        self.assertEqual(str(metadata["resource"]).rstrip("/"), MCP_ENDPOINT)

        servers = metadata.get("authorization_servers") or []
        self.assertTrue(servers, "the document sends a client nowhere to sign in")
        for server in servers:
            self.assertTrue(
                str(server).startswith("https://login.microsoftonline.com/"),
                f"unexpected authorization server: {server}",
            )
        if TENANT_ID:
            # Guarded rather than required: AZURE_TENANT_ID comes from the same
            # azd environment as everything else, and is absent only when the
            # harness is pointed at an endpoint by hand.
            self.assertTrue(
                any(TENANT_ID in str(server) for server in servers),
                f"the document names {servers}, not tenant {TENANT_ID}",
            )


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class TheWarehouseAdmitsEverySeededOwner(unittest.TestCase):
    """The grant this harness cannot feel the absence of, checked directly.

    Unity Catalog's SELECT is half of what a second person needs. A SQL
    warehouse carries its own ACL, and running a statement on it takes CAN_USE
    or better. Without that grant the colleague the two-person demo is built
    around gets `databricks_list_tables` working -- it is a control-plane call
    -- and `databricks_query_orders` failing, which reads as a bug in the
    accelerator rather than as a missing permission.

    The harness only ever runs as whoever deployed, who created the warehouse
    and holds this implicitly, so no query it can make would notice the grant
    disappear. So this asserts the grant rather than its effect: it reads the
    ACL and looks for the seeded owners in it. Proving they can actually run a
    statement needs a second signed-in identity, which the harness does not
    have — that half stays a manual check, and it is the one worth doing before
    showing this to anyone.
    """

    def test_every_seeded_owner_may_run_queries_on_the_warehouse(self) -> None:
        if not WAREHOUSE_ID:
            self.skipTest("DATABRICKS_WAREHOUSE_ID is not in this environment")
        # The bootstrap skips the deploying user for the same reason: creating
        # the warehouse made them its owner, so there is nothing to grant.
        deployer = signed_in_upn().lower()
        expected = [owner for owner in DEMO_OWNERS if owner != deployer]
        if not expected:
            self.skipTest(
                "a single-owner deployment grants nobody warehouse access; set "
                "DATABRICKS_DEMO_OWNERS and re-run the bootstrap to exercise this"
            )

        acl = (
            workspace_get(self, f"/api/2.0/permissions/warehouses/{WAREHOUSE_ID}") or {}
        )
        held = {
            str(entry["user_name"]).lower(): {
                str(permission.get("permission_level") or "")
                for permission in entry.get("all_permissions") or []
            }
            for entry in acl.get("access_control_list") or []
            if entry.get("user_name")
        }

        for owner in expected:
            self.assertIn(
                owner,
                held,
                f"{owner} was seeded rows but holds nothing on the warehouse, so "
                "they can list tables and not query them",
            )
            self.assertTrue(
                held[owner] & RUN_QUERY_PERMISSIONS,
                f"{owner} holds {sorted(held[owner])} on the warehouse, none of "
                "which permits running a statement",
            )


if __name__ == "__main__":
    unittest.main()
