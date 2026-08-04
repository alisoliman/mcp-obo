"""The three loops that only exist because Databricks answers slowly."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from fastmcp.exceptions import ToolError

from src.mcp_server import databricks


class DatabricksTransportTests(unittest.IsolatedAsyncioTestCase):
    """The three loops that only exist because Databricks answers slowly.

    Polling, retrying and paging are the corrections a real workspace forces and
    a demo workspace hides: a warm warehouse answers in the POST, a schema with
    one table never pages, and a control plane that is not busy never says 429.
    So none of them is exercised by using the accelerator, and all three are
    reached the moment someone points it at their own data. They are driven here
    against a scripted transport rather than a workspace, which proves the
    sequence of calls and nothing about Databricks itself.
    """

    settings = databricks.DatabricksSettings(
        host="https://adb-1234567890.11.azuredatabricks.net",
        warehouse_id="0123456789abcdef",
        catalog="mcp_obo_demo",
        schema_name="sales",
    )

    def client(self, handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_a_statement_is_polled_rather_than_cancelled_under_it(self) -> None:
        # The warehouse answers PENDING, then RUNNING, then the rows. Sending
        # on_wait_timeout=CANCEL, as this once did, turned exactly this sequence
        # into a CANCELED with no reason attached for whoever asked first after
        # the ten-minute auto-stop.
        seen: list[tuple[str, str]] = []
        states = iter(["PENDING", "RUNNING"])

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["on_wait_timeout"], "CONTINUE")
                return httpx.Response(
                    200,
                    json={"statement_id": "abcd-1234", "status": {"state": "PENDING"}},
                )
            state = next(states, "SUCCEEDED")
            if state != "SUCCEEDED":
                return httpx.Response(200, json={"status": {"state": state}})
            return httpx.Response(
                200,
                json={
                    "statement_id": "abcd-1234",
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {"schema": {"columns": [{"name": "ran_as"}]}},
                    "result": {"data_array": [["ada@contoso.com"]]},
                },
            )

        async with self.client(handler) as client:
            with (
                patch("src.mcp_server.databricks._POLL_FIRST", 0),
                patch("src.mcp_server.databricks._POLL_CAP", 0),
            ):
                body = await databricks._run_statement(
                    client, self.settings, "token", "SELECT current_user()"
                )

        self.assertEqual(databricks._rows_from(body)[1], [["ada@contoso.com"]])
        self.assertEqual(seen[0], ("POST", "/api/2.0/sql/statements"))
        self.assertEqual(seen[1:], [("GET", "/api/2.0/sql/statements/abcd-1234")] * 3)

    async def test_a_warehouse_that_never_answers_is_handed_back_not_abandoned(
        self,
    ) -> None:
        # The budget is what separates "still starting" from "denied", and the
        # cancel is what stops this from walking away from a statement the
        # warehouse is still running. `on_wait_timeout` is CONTINUE, so nothing
        # else will stop it -- and the API exposes cancellation as a POST to
        # .../cancel, not as a DELETE on the statement, which is a mistake that
        # would return a perfectly quiet 404.
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path.endswith("/cancel"):
                return httpx.Response(200, json={})
            return httpx.Response(
                200, json={"statement_id": "abcd-1234", "status": {"state": "PENDING"}}
            )

        async with self.client(handler) as client:
            with patch("src.mcp_server.databricks._STATEMENT_BUDGET", -1):
                with self.assertRaises(ToolError) as raised:
                    await databricks._run_statement(
                        client, self.settings, "token", "SELECT 1"
                    )

        message = str(raised.exception)
        self.assertIn("Retry shortly", message)
        # Said in as many words, because the model reads this and a warehouse
        # starting must not be summarised as Unity Catalog refusing.
        self.assertIn("not an authorization decision", message)
        self.assertIn(("POST", "/api/2.0/sql/statements/abcd-1234/cancel"), seen)

    async def test_a_statement_with_no_usable_id_is_not_called_a_timeout(self) -> None:
        # Nothing to poll and nothing to cancel is a different failure from a
        # slow warehouse, and telling the model the statement "was cancelled
        # after 90s" when neither happened is the kind of confident wrong answer
        # this whole pass exists to remove.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"statement_id": "?", "status": {"state": "PENDING"}}
            )

        async with self.client(handler) as client:
            with self.assertRaises(ToolError) as raised:
                await databricks._run_statement(
                    client, self.settings, "token", "SELECT 1"
                )

        self.assertIn("no usable statement id", str(raised.exception))
        self.assertNotIn("Retry shortly", str(raised.exception))

    async def test_a_throttle_is_retried_and_a_denial_is_not(self) -> None:
        attempts: list[int] = []

        def throttled(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={})
            return httpx.Response(200, json={"ok": True})

        async with self.client(throttled) as client:
            body = await databricks._request(
                client, self.settings, "token", "GET", "/api/2.0/preview/scim/v2/Me"
            )
        self.assertEqual(body, {"ok": True})
        self.assertEqual(len(attempts), 3)

        # A Unity Catalog denial is the demonstration. Retrying it would delay
        # it and blur it into a transient fault.
        denials: list[int] = []

        def denied(request: httpx.Request) -> httpx.Response:
            denials.append(1)
            return httpx.Response(
                403,
                json={
                    "error_code": "PERMISSION_DENIED",
                    "message": "User does not have SELECT on\r\nTable 'orders'",
                },
            )

        async with self.client(denied) as client:
            with self.assertRaises(ToolError) as raised:
                await databricks._request(
                    client,
                    self.settings,
                    "token",
                    "GET",
                    "/api/2.1/unity-catalog/tables",
                )

        self.assertEqual(len(denials), 1)
        # The extracted error_code and message, not the JSON body they came in:
        # this string is logged by both pods, so the carriage return a response
        # can carry must not survive to forge a second log record, and the
        # braces and quotes of the raw body must not be there to read around.
        self.assertEqual(
            str(raised.exception),
            "Databricks returned 403: PERMISSION_DENIED User does not have "
            "SELECT on Table 'orders'",
        )

    async def test_an_empty_page_carrying_a_token_is_not_the_end_of_the_list(
        self,
    ) -> None:
        # Databricks documents this exactly: a page may hold zero results and
        # still carry a token. Stopping on the empty page would report "you may
        # see nothing", which is the one answer this accelerator must never give
        # by accident.
        pages = iter(
            [
                {"tables": [], "next_page_token": "p2"},
                {"tables": [{"full_name": "c.s.orders"}], "next_page_token": "p3"},
                {"tables": [{"full_name": "c.s.audit"}]},
            ]
        )
        tokens: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            tokens.append(request.url.params.get("page_token"))
            return httpx.Response(200, json=next(pages))

        async with self.client(handler) as client:
            listing = await databricks._list_tables(client, self.settings, "token")

        self.assertEqual(
            [table.full_name for table in listing.tables], ["c.s.orders", "c.s.audit"]
        )
        self.assertFalse(listing.more_available)
        self.assertEqual(tokens, [None, "p2", "p3"])

    async def test_a_list_stopped_by_the_page_cap_says_so(self) -> None:
        # Every page carries a token, so the walk is cut short by _MAX_PAGES
        # rather than by Databricks. Reporting that as a complete listing is how
        # a model comes to tell someone a schema holds less than it does.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"tables": [{"full_name": "c.s.t"}], "next_page_token": "more"},
            )

        async with self.client(handler) as client:
            with patch("src.mcp_server.databricks._MAX_PAGES", 3):
                listing = await databricks._list_tables(client, self.settings, "token")

        self.assertEqual(len(listing.tables), 3)
        self.assertTrue(listing.more_available)


if __name__ == "__main__":
    unittest.main()
