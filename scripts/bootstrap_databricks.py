"""Create the Unity Catalog objects that ARM cannot.

Bicep stops at the workspace. Metastores are an *account*-level Databricks
concept, and catalogs, tables, warehouses, grants, row filters and column masks
are all workspace-level REST or SQL. This script covers that gap and is the only
imperative step in the deployment.

It runs as the deploying user through `AzureCliCredential` and is idempotent:
the demo tables are dropped and rebuilt so a re-run lands on known seed data. It
grants nothing to the MCP server's own identity -- the server reaches this data
only while holding a caller's token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

from src.obo import DATABRICKS_SCOPE

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class BootstrapError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def required(name: str) -> str:
    value = env(name)
    if not value:
        raise BootstrapError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    workspace_name: str
    workspace_resource_id: str
    catalog: str
    schema: str
    warehouse_name: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            host=required("DATABRICKS_HOST").rstrip("/"),
            workspace_name=env("DATABRICKS_WORKSPACE_NAME"),
            workspace_resource_id=env("DATABRICKS_WORKSPACE_RESOURCE_ID"),
            catalog=required("DATABRICKS_CATALOG"),
            schema=required("DATABRICKS_SCHEMA"),
            warehouse_name=required("DATABRICKS_WAREHOUSE_NAME"),
        )


class Databricks:
    def __init__(
        self,
        token: str,
        arm_token: str = "",
        workspace_resource_id: str = "",
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        if arm_token and workspace_resource_id:
            # An Entra token alone is not enough: the identity has to exist in
            # the workspace, and a fresh one only knows whoever created it.
            # These headers let anyone with Owner or Contributor on the
            # workspace resource provision themselves on first call. Without
            # them the bootstrap fails with "Unauthorized access to Org".
            headers["X-Databricks-Azure-SP-Management-Token"] = arm_token
            headers["X-Databricks-Azure-Workspace-Resource-Id"] = workspace_resource_id
        self._client = httpx.Client(timeout=TIMEOUT, headers=headers)

    def call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow: tuple[int, ...] = (),
    ) -> dict[str, Any] | None:
        """Return the parsed body, or None when the status was in `allow`.

        `allow` exists so "does this already exist?" reads as `is None` at the
        call site rather than as a sentinel dict pretending to be a response.
        """
        response = self._client.request(method, url, params=params, json=json_body)
        if response.status_code in allow:
            return None
        if response.status_code >= 400:
            raise BootstrapError(
                f"{method} {url} -> {response.status_code}: {response.text[:500]}"
            )
        return response.json() if response.content else {}

    def close(self) -> None:
        self._client.close()


def log(message: str) -> None:
    # stderr, so `--print-env` leaves stdout clean enough to pipe into
    # `azd env set`.
    print(f"  {message}", file=sys.stderr, flush=True)


def ensure_metastore(api: Databricks, config: Config) -> None:
    """Confirm a Unity Catalog metastore is assigned.

    On Azure one is auto-provisioned with the first workspace in a region, which
    is why this accelerator needs no Databricks account administrator. Creating
    one is an account-level operation, so a tenant with that automatic
    assignment switched off has to do it by hand before running this.
    """
    current = api.call(
        "GET",
        f"{config.host}/api/2.1/unity-catalog/metastore_summary",
        allow=(404, 400),
    )
    if current and "metastore_id" in current:
        log(f"metastore already assigned: {current['metastore_id']}")
        return

    raise BootstrapError(
        "This workspace has no Unity Catalog metastore. Creating and assigning\n"
        "one is an account-level operation: sign in to\n"
        "https://accounts.azuredatabricks.net as an Entra Global Administrator,\n"
        "create a metastore in this region, assign it to the workspace, then\n"
        "re-run. See docs/databricks-mode.md.\n"
    )


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def ensure_catalog(api: Databricks, config: Config) -> str:
    """Return a catalog to build the demo in, preferring the requested name.

    The metastore Azure auto-provisions has no storage root of its own --
    Databricks holds the storage behind "Default Storage" instead -- so a plain
    CREATE CATALOG fails with INVALID_STATE unless you name a location. Rather
    than demand an ADLS container just to run a demo, this falls back to the
    managed catalog Databricks already created alongside the workspace.
    """
    uc = f"{config.host}/api/2.1/unity-catalog"

    if api.call("GET", f"{uc}/catalogs/{config.catalog}", allow=(404,)) is not None:
        log(f"catalog already exists: {config.catalog}")
        return config.catalog

    created = api.call(
        "POST",
        f"{uc}/catalogs",
        json_body={
            "name": config.catalog,
            "comment": "MCP OBO delegated identity demo",
        },
        allow=(400, 403),
    )
    if created is not None:
        log(f"created catalog {config.catalog}")
        return config.catalog

    managed = [
        str(catalog["name"])
        for catalog in (api.call("GET", f"{uc}/catalogs") or {}).get("catalogs", [])
        if catalog.get("catalog_type") == "MANAGED_CATALOG"
    ]
    if managed:
        # Databricks names the workspace's own catalog after the workspace, so
        # prefer that over whichever happens to sort first.
        expected = config.workspace_name.replace("-", "_").lower()
        name = next((c for c in managed if c.lower() == expected), managed[0])
        log(
            "no storage root for a new catalog; using the workspace's "
            f"managed catalog {name}"
        )
        return name

    raise BootstrapError(
        f"Could not create catalog {config.catalog} and found no managed "
        "catalog to fall back to. Create one in the Databricks UI with Default "
        "Storage, then set DATABRICKS_CATALOG to its name and re-run."
    )


def ensure_warehouse(api: Databricks, config: Config) -> str:
    listed = api.call("GET", f"{config.host}/api/2.0/sql/warehouses")
    for warehouse in listed.get("warehouses", []):
        if warehouse.get("name") == config.warehouse_name:
            warehouse_id = str(warehouse["id"])
            log(f"warehouse {config.warehouse_name} already exists: {warehouse_id}")
            return start_warehouse(api, config, warehouse_id)

    payload = {
        "name": config.warehouse_name,
        "cluster_size": "2X-Small",
        "min_num_clusters": 1,
        "max_num_clusters": 1,
        "auto_stop_mins": 10,
        "warehouse_type": "PRO",
        "enable_serverless_compute": True,
        "spot_instance_policy": "COST_OPTIMIZED",
    }
    try:
        created = api.call(
            "POST", f"{config.host}/api/2.0/sql/warehouses", json_body=payload
        )
    except BootstrapError as error:
        # Serverless is not available in every region or account. Classic PRO
        # warehouses enforce Unity Catalog identically; they just start slower.
        log(f"serverless unavailable, falling back to classic PRO ({error})")
        payload["enable_serverless_compute"] = False
        created = api.call(
            "POST", f"{config.host}/api/2.0/sql/warehouses", json_body=payload
        )

    warehouse_id = str(created["id"])
    log(f"created warehouse {config.warehouse_name}: {warehouse_id}")
    return start_warehouse(api, config, warehouse_id)


def start_warehouse(api: Databricks, config: Config, warehouse_id: str) -> str:
    url = f"{config.host}/api/2.0/sql/warehouses/{warehouse_id}"
    state = api.call("GET", url).get("state", "")
    if state not in {"RUNNING", "STARTING"}:
        api.call("POST", f"{url}/start", allow=(400,))

    deadline = time.time() + 600
    while time.time() < deadline:
        state = api.call("GET", url).get("state", "")
        if state == "RUNNING":
            log(f"warehouse running: {warehouse_id}")
            return warehouse_id
        if state in {"DELETED", "STOPPED"}:
            raise BootstrapError(f"warehouse entered {state} while starting")
        time.sleep(10)
    raise BootstrapError("warehouse did not reach RUNNING within 10 minutes")


def run_sql(
    api: Databricks, config: Config, warehouse_id: str, statement: str
) -> None:
    body = api.call(
        "POST",
        f"{config.host}/api/2.0/sql/statements",
        json_body={
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": "50s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        },
    )
    statement_id = body.get("statement_id")
    state = body.get("status", {}).get("state")

    deadline = time.time() + 600
    while state in {"PENDING", "RUNNING"} and time.time() < deadline:
        time.sleep(3)
        body = api.call(
            "GET", f"{config.host}/api/2.0/sql/statements/{statement_id}"
        )
        state = body.get("status", {}).get("state")

    if state != "SUCCEEDED":
        message = body.get("status", {}).get("error", {}).get("message", "")
        raise BootstrapError(
            f"statement {state}: {message}\n  ---\n  {statement.strip()[:400]}"
        )


def current_user(api: Databricks, config: Config) -> str:
    me = api.call("GET", f"{config.host}/api/2.0/preview/scim/v2/Me")
    user = me.get("userName", "")
    if not user:
        raise BootstrapError("Databricks did not resolve a user for this token")
    return user


def demo_owners(user: str) -> list[str]:
    """Who the seeded orders belong to.

    The bootstrapping user always comes first. `DATABRICKS_DEMO_OWNERS` adds
    further real sign-ins, so two people can run the identical query and get
    different rows. A synthetic owner is always appended so there is a row the
    caller cannot see even when only one real user is seeded.
    """
    owners = [user]
    seen = {user.lower()}
    for owner in (o.strip() for o in env("DATABRICKS_DEMO_OWNERS").split(",")):
        if owner and owner.lower() not in seen:
            owners.append(owner)
            seen.add(owner.lower())
    owners.append("dana@contoso.example")
    return owners


def seed(
    api: Databricks, config: Config, warehouse_id: str, catalog: str, user: str
) -> tuple[int, int]:
    schema = config.schema
    qualified = f"{catalog}.{schema}"
    orders = f"{qualified}.orders"
    row_filter = f"{qualified}.orders_row_filter"
    mask = f"{qualified}.mask_email"

    def sql(statement: str) -> None:
        run_sql(api, config, warehouse_id, statement)

    sql(f"CREATE SCHEMA IF NOT EXISTS {qualified}")
    # Unity Catalog privileges are separate from workspace admin: a schema
    # created by one operator cannot be rebuilt by another until ownership
    # moves. Claiming it is harmless when the caller already owns it.
    try:
        sql(f"ALTER SCHEMA {qualified} OWNER TO `{user}`")
    except BootstrapError as error:
        log(f"could not take ownership of {qualified}: {error}")
    log(f"schema ready: {qualified}")

    # Dropping first detaches any existing filter and mask, so the functions can
    # be replaced.
    sql(f"DROP TABLE IF EXISTS {orders}")

    sql(
        f"CREATE OR REPLACE FUNCTION {row_filter}(owner STRING) "
        "RETURNS BOOLEAN RETURN owner = current_user()"
    )
    # Unconditional on purpose. A real deployment would exempt a group here --
    # `CASE WHEN is_account_group_member('auditors') THEN email ELSE ... END` --
    # but creating an account-level group needs a Databricks account admin.
    sql(
        f"CREATE OR REPLACE FUNCTION {mask}(email STRING) RETURNS STRING "
        "RETURN concat('***@', split_part(email, '@', 2))"
    )
    log("row filter and column mask functions created")

    sql(
        f"CREATE TABLE {orders} ("
        "order_id STRING, owner_email STRING, region STRING, "
        "customer_email STRING, amount_cents BIGINT) "
        "COMMENT 'Row filtered by owner_email = current_user()'"
    )

    # Three orders per owner, two of them in EU, so the per-user counts the
    # end-to-end harness asserts on hold however many owners are seeded.
    owners = demo_owners(user)
    regions = ("EU", "EU", "APAC")
    rows = []
    for owner in owners:
        for region in regions:
            number = len(rows) + 1
            rows.append(
                (
                    f"ORD-{number:06d}",
                    owner,
                    region,
                    f"customer{number}@northwind.example",
                    10000 + number * 1111,
                )
            )
    values = ", ".join(
        "('{}', '{}', '{}', '{}', {})".format(*row) for row in rows
    )
    sql(f"INSERT INTO {orders} VALUES {values}")
    per_owner = len(regions)
    log(f"seeded {len(rows)} rows across {len(owners)} owners, {per_owner} each")
    for owner in owners:
        log(f"  owner: {owner}")

    sql(f"ALTER TABLE {orders} SET ROW FILTER {row_filter} ON (owner_email)")
    sql(f"ALTER TABLE {orders} ALTER COLUMN customer_email SET MASK {mask}")
    log("row filter and column mask attached")

    # Grants a non-admin consumer needs. The deployer is usually the catalog
    # owner and already has these implicitly; they are stated so a customer can
    # point the demo at a normal user and have it work unchanged.
    sql(f"GRANT USE CATALOG ON CATALOG {catalog} TO `{user}`")
    sql(f"GRANT USE SCHEMA ON SCHEMA {qualified} TO `{user}`")
    sql(f"GRANT SELECT ON TABLE {orders} TO `{user}`")
    for owner in owners:
        if owner == user or owner.endswith("contoso.example"):
            continue
        # A listed owner who is not provisioned in this workspace cannot be
        # granted, and that should not fail the bootstrap.
        try:
            sql(f"GRANT USE CATALOG ON CATALOG {catalog} TO `{owner}`")
            sql(f"GRANT USE SCHEMA ON SCHEMA {qualified} TO `{owner}`")
            sql(f"GRANT SELECT ON TABLE {orders} TO `{owner}`")
            log(f"granted SELECT on orders to {owner}")
        except BootstrapError as error:
            log(f"could not grant to {owner} (not provisioned?): {error}")
    log("grants applied to orders")
    return len(rows), per_owner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print the resolved values as KEY=value for azd env set",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help=(
            "Provision the catalog, schema and warehouse but skip the demo "
            "orders, filters and grants. Use this when pointing the MCP server "
            "at real data instead of the sample."
        ),
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    credential = AzureCliCredential()
    arm_token = ""
    if config.workspace_resource_id:
        try:
            arm_token = credential.get_token(
                "https://management.core.windows.net/.default"
            ).token
        except ClientAuthenticationError as error:
            log(f"no ARM token ({error}); continuing without workspace elevation")
    api = Databricks(
        credential.get_token(DATABRICKS_SCOPE).token,
        arm_token,
        config.workspace_resource_id,
    )
    total = per_owner = 0
    try:
        log(f"bootstrapping Unity Catalog on {config.host}")
        user = current_user(api, config)
        log(f"acting as {user}")
        ensure_metastore(api, config)
        catalog = ensure_catalog(api, config)
        warehouse_id = ensure_warehouse(api, config)
        if args.no_seed:
            log("--no-seed: leaving the schema contents alone")
        else:
            total, per_owner = seed(api, config, warehouse_id, catalog, user)
    except BootstrapError as error:
        print(f"\nerror: {error}", file=sys.stderr)
        return 1
    finally:
        api.close()

    # The catalog is echoed back because the fallback may have chosen a
    # different one than was asked for. The seeded totals vary with
    # DATABRICKS_DEMO_OWNERS, and the harness asserts against them.
    resolved = {
        "DATABRICKS_WAREHOUSE_ID": warehouse_id,
        "DATABRICKS_CATALOG": catalog,
    }
    if not args.no_seed:
        resolved["DATABRICKS_SEEDED_TOTAL"] = str(total)
        resolved["DATABRICKS_SEEDED_PER_OWNER"] = str(per_owner)
    log("Unity Catalog is ready.")
    if args.print_env:
        for key, value in resolved.items():
            print(f"{key}={value}")
    else:
        print(json.dumps(resolved, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
