"""What the compiled Bicep template asks Entra for."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from typing import Any

from src.mcp_server.obo import (
    DATABRICKS_RESOURCE_APP_ID,
    DATABRICKS_SCOPE,
    GRAPH_SCOPE,
)
from tests.helpers import (
    ARM_ACCESS_TYPE,
    BICEP_ABSENT,
    ROOT,
)


class EntraPermissionTests(unittest.TestCase):
    """The permission model, read off the template rather than the prose."""

    def test_the_two_downstream_scopes_differ_only_by_resource(self) -> None:
        self.assertEqual(GRAPH_SCOPE, "https://graph.microsoft.com/.default")
        self.assertEqual(DATABRICKS_SCOPE, f"{DATABRICKS_RESOURCE_APP_ID}/.default")
        # A fixed Azure constant, identical in every tenant.
        self.assertEqual(
            DATABRICKS_RESOURCE_APP_ID, "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
        )

    def compiled_arm_template(self) -> dict[str, Any]:
        """infra/main.bicep as ARM JSON, or a skip if there is no bicep here.

        A failed compile is only a reason to skip when the toolchain is missing.
        A template that no longer compiles is exactly the kind of breakage the
        test below exists to catch, so it fails rather than reporting itself
        green-by-omission.
        """
        if shutil.which("az") is None:
            self.skipTest("the az CLI is not installed")
        compiled = subprocess.run(
            [
                "az",
                "bicep",
                "build",
                "--file",
                str(ROOT / "infra" / "main.bicep"),
                "--stdout",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if compiled.returncode != 0:
            stderr = compiled.stderr.strip()
            missing = "Error BCP" not in stderr and any(
                phrase in stderr.lower() for phrase in BICEP_ABSENT
            )
            if missing:
                self.skipTest(f"az bicep is unavailable: {stderr[:200]}")
            self.fail(f"infra/main.bicep does not compile:\n{stderr}")
        return json.loads(compiled.stdout)

    def test_no_application_permission_is_ever_requested(self) -> None:
        # Compiled, not grepped. `type: 'Role'` is how a Bicep resourceAccess
        # entry asks for an application permission -- one that works with no user
        # present -- and the whole accelerator rests on there being none, for
        # Graph or for Databricks. Asserting against the ARM JSON means a
        # rename or a refactor cannot quietly turn this green.
        #
        # Both shapes the compiler emits have to be read. A literal list stays
        # JSON, but the server application's list is a mode-dependent
        # concat/if and compiles to a single ARM expression string, where an
        # entry reads as `'type', 'Scope'` rather than as an object. Walking
        # only the dicts skips the one list that carries the Databricks
        # permission -- which is to say, it skips the claim being made here.
        template = self.compiled_arm_template()
        resources = template["resources"]
        entries = resources.values() if isinstance(resources, dict) else resources
        declared = [
            resource.get("properties", {}).get("requiredResourceAccess")
            for resource in entries
            if str(resource.get("type", "")).startswith("Microsoft.Graph/applications")
        ]

        literal = [
            access
            for required in declared
            if isinstance(required, list)
            for entry in required
            for access in (entry.get("resourceAccess") or [])
        ]
        # An expression that references another application's list rather than
        # declaring one carries no `'type'` pair, and following it would only
        # double-count the declaration it points at.
        expressions = [
            required
            for required in declared
            if isinstance(required, str) and ARM_ACCESS_TYPE.search(required)
        ]
        self.assertEqual(len(expressions), 1, "expected one mode-dependent list")

        requested = [access.get("type") for access in literal]
        requested += ARM_ACCESS_TYPE.findall(expressions[0])
        # Three: Graph User.Read and Databricks user_impersonation on the server
        # application, one per mode, and the client's scope on this server.
        self.assertEqual(len(requested), 3, f"unexpected permissions: {declared}")
        for permission in requested:
            self.assertEqual(
                permission, "Scope", f"application permission found: {declared}"
            )

        # And both downstream resources are actually asked for, each as a
        # delegated permission. Losing a branch of that if would otherwise leave
        # this test passing on the remaining one.
        for scope_variable in (
            "graphUserReadScopeId",
            "databricksUserImpersonationScopeId",
        ):
            self.assertRegex(
                expressions[0],
                rf"variables\('{scope_variable}'\),\s*'type',\s*'Scope'",
            )

        # Permission ids compile to variable references, so the values are
        # checked where the compiler resolved them.
        self.assertEqual(
            template["variables"]["databricksUserImpersonationScopeId"],
            "739272be-e143-11e8-9f32-f2801f1b9fd1",
        )
        self.assertEqual(
            template["variables"]["databricksAppId"],
            "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d",
        )
