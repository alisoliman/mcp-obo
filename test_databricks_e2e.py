"""End-to-end proof that the delegated identity reaches Unity Catalog.

Everything here runs against the deployed stack. It signs in as a real user,
calls the real MCP endpoint over HTTPS, and asserts on what Databricks sent
back. Nothing is mocked, because the claim being tested -- that Unity Catalog,
not this codebase, decides which rows come back -- cannot be tested any other
way.

Run it after `azd up`:

    uv run python -m unittest test_databricks_e2e -v

The whole module skips when the environment is not configured, so it is safe to
leave in the default `python -m unittest` discovery run.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import unittest

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


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
MCP_ENDPOINT = ENV.get("MCP_ENDPOINT", "").strip()
SERVER_APP_ID = ENV.get("ENTRA_SERVER_CLIENT_ID", "").strip()
DATABRICKS_HOST = ENV.get("DATABRICKS_HOST", "").strip()
# Databricks mode only. An orders-mode deployment serves none of these tools, so
# the whole module skips rather than failing on tools that were never meant to
# be there.
MODE = (ENV.get("MCP_MODE", "orders").strip().lower() or "orders")
CONFIGURED = bool(
    MODE == "databricks" and MCP_ENDPOINT and SERVER_APP_ID and DATABRICKS_HOST
)

# Written back by the bootstrap, so these follow however many owners were
# seeded rather than drifting from a constant.
SEEDED_TOTAL = int(ENV.get("DATABRICKS_SEEDED_TOTAL", "0") or 0)
SEEDED_FOR_USER = int(ENV.get("DATABRICKS_SEEDED_PER_OWNER", "3") or 3)
SEEDED_FOR_USER_IN_EU = 2


def run(command: list[str]) -> str:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=180, check=True
    ).stdout.strip()


def run_graph(test: unittest.TestCase, command: list[str]) -> str:
    """Run an `az` command that reaches Microsoft Graph.

    Activating a PIM role changes conditional-access policy, and Microsoft Graph
    then rejects tokens minted before the change with a Continuous Access
    Evaluation challenge. That is a stale local login, not a defect in the
    deployment, so it skips with instructions instead of reporting a failure.
    Every other error still fails.
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
    the server application. That is opt-in and off by default -- see the note in
    infra/main.bicep. Nothing in the running system depends on it.

    Cached for the run: the token is valid for an hour and shelling out to `az`
    costs about a second each time.
    """
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = run(
            [
                "az", "account", "get-access-token",
                "--resource", f"api://{SERVER_APP_ID}",
                "--query", "accessToken", "-o", "tsv",
            ]
        )
    return _TOKEN


_TOKEN: str | None = None


def signed_in_upn() -> str:
    """The signed-in user, read from the local CLI profile.

    Deliberately not `az ad signed-in-user show`: that reaches Microsoft Graph,
    which makes the identity assertions depend on a second service having a
    healthy token. The profile already knows who is signed in.
    """
    return run(["az", "account", "show", "--query", "user.name", "-o", "tsv"])


async def call(tool: str, arguments: dict | None = None):
    transport = StreamableHttpTransport(
        url=MCP_ENDPOINT, headers={"Authorization": f"Bearer {user_token()}"}
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments or {})

    # `result.data` is a generated model rather than a mapping, so the tests
    # read the structured payload. FastMCP wraps a non-object return value —
    # a list of tables, say — in a single `result` key.
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class DelegatedIdentityReachesDatabricks(unittest.TestCase):
    """The identity survives every hop between the browser and the warehouse."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.upn = signed_in_upn()
        # One query, shared by the assertions that read different columns of
        # it. Re-running it per test would mean another MCP session, another
        # OBO exchange and another warehouse statement for the same answer.
        cls.orders = asyncio.run(call("databricks_query_orders"))

    def test_databricks_resolves_the_signed_in_user_not_the_server(self) -> None:
        identity = asyncio.run(call("databricks_whoami"))

        # SCIM answers from the Databricks control plane; current_user() answers
        # from the SQL warehouse. Both naming the same human is the proof that
        # the delegated token survived the OBO exchange and both hops beyond it.
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
        tables = asyncio.run(call("databricks_list_tables"))
        names = {table["full_name"].split(".")[-1] for table in tables}
        self.assertIn("orders", names)


@unittest.skipUnless(CONFIGURED, "not a databricks-mode deployment; run after `azd up`")
class NoApplicationOnlyPathExists(unittest.TestCase):
    """The negative half of the claim, checked against live Entra ID.

    These assertions hold no matter who runs them. A metastore admin bypasses
    most Unity Catalog grants, so 'the admin cannot read table X' would be a
    weak test; 'the server holds no permission that works without a user' is
    the property the accelerator actually rests on.
    """

    def test_the_server_holds_no_application_permission(self) -> None:
        permissions = json.loads(
            run_graph(self, [
                "az", "ad", "app", "permission", "list",
                "--id", SERVER_APP_ID, "-o", "json",
            ])
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
            run_graph(self, [
                "az", "rest", "--method", "GET", "--url",
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal['id']}"
                "/appRoleAssignments", "-o", "json",
            ])
        )
        self.assertEqual(
            assignments.get("value", []),
            [],
            "the MCP server has been granted an application permission",
        )

    def test_the_endpoint_rejects_an_unauthenticated_call(self) -> None:
        response = httpx.post(
            MCP_ENDPOINT,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
