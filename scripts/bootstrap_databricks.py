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
from urllib.parse import quote as url_quote

import httpx
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

from src.databricks import IDENTIFIER
from src.obo import DATABRICKS_SCOPE

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
ARM_SCOPE = "https://management.core.windows.net/.default"
SYNTHETIC_OWNER = "dana@contoso.example"
# Renew this far ahead of expiry, so a token cannot lapse between the header
# being built and Databricks reading it.
TOKEN_SKEW_SECONDS = 300


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
        config = cls(
            host=required("DATABRICKS_HOST").rstrip("/"),
            workspace_name=env("DATABRICKS_WORKSPACE_NAME"),
            workspace_resource_id=env("DATABRICKS_WORKSPACE_RESOURCE_ID"),
            catalog=required("DATABRICKS_CATALOG"),
            schema=required("DATABRICKS_SCHEMA"),
            warehouse_name=required("DATABRICKS_WAREHOUSE_NAME"),
        )
        # The same check `DatabricksSettings.from_env` applies to these two
        # values when the server boots, imported rather than restated so the two
        # cannot drift. Applying it here means a name that would later stop the
        # server from starting never reaches CREATE SCHEMA, and a name that
        # would need quoting never reaches the REST path below.
        for name, value in (
            ("DATABRICKS_CATALOG", config.catalog),
            ("DATABRICKS_SCHEMA", config.schema),
        ):
            if not IDENTIFIER.fullmatch(value):
                raise BootstrapError(
                    f"{name}={value!r} is not a valid Unity Catalog identifier "
                    "(letters, digits and underscore, up to 128 characters)"
                )
        return config


class Databricks:
    def __init__(
        self,
        credential: TokenCredential,
        workspace_resource_id: str = "",
    ) -> None:
        # The credential is held rather than a token snapshot. Entra tokens last
        # 60-90 minutes and one bootstrap can outlive that: starting a classic
        # warehouse is allowed ten minutes on its own, and every statement after
        # it another ten.
        self._credential = credential
        self._workspace_resource_id = workspace_resource_id
        self._elevate = bool(workspace_resource_id)
        self._tokens: dict[str, AccessToken] = {}
        self._client = httpx.Client(timeout=TIMEOUT)

    def _token(self, scope: str) -> str:
        """A live token for `scope`, re-fetched only as it nears expiry.

        Cached here rather than left to the credential: AzureCliCredential shells
        out to `az account get-access-token` on every call, with no in-process
        cache, so asking per request would put a subprocess spawn in front of
        every poll of a running statement.
        """
        cached = self._tokens.get(scope)
        if cached is None or cached.expires_on - TOKEN_SKEW_SECONDS <= time.time():
            cached = self._credential.get_token(scope)
            self._tokens[scope] = cached
        return cached.token

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token(DATABRICKS_SCOPE)}"}
        if self._elevate:
            # An Entra token alone is not enough: the identity has to exist in
            # the workspace, and a fresh one only knows whoever created it.
            # These headers let anyone with Owner or Contributor on the
            # workspace resource provision themselves on first call. Without
            # them the bootstrap fails with "Unauthorized access to Org".
            try:
                arm = self._token(ARM_SCOPE)
            except ClientAuthenticationError as error:
                self._elevate = False
                log(f"no ARM token ({error}); continuing without workspace elevation")
            else:
                headers["X-Databricks-Azure-SP-Management-Token"] = arm
                headers["X-Databricks-Azure-Workspace-Resource-Id"] = (
                    self._workspace_resource_id
                )
        return headers

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
        response = self._client.request(
            method, url, params=params, json=json_body, headers=self._headers()
        )
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
    # Encoded because it lands in a path segment. The identifier check in
    # `Config.from_env` already rules out anything that would need it, so this
    # is here to keep the two facts independent rather than to fix a live bug.
    name = url_quote(config.catalog, safe="")

    if api.call("GET", f"{uc}/catalogs/{name}", allow=(404,)) is not None:
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
        # A name the server's own identifier check would reject is no fallback
        # at all: it would be written into the environment and the server would
        # refuse to boot on it.
        and IDENTIFIER.fullmatch(str(catalog.get("name", "")))
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
    deadline = time.time() + 600
    starts = 0
    while time.time() < deadline:
        state = api.call("GET", url).get("state", "")
        if state == "RUNNING":
            log(f"warehouse running: {warehouse_id}")
            return warehouse_id
        if state == "DELETED":
            raise BootstrapError("warehouse was deleted while starting")
        if state == "STOPPED":
            # With a ten-minute auto-stop, catching this warehouse shutting down
            # is routine, and a warehouse in STOPPING passes through STOPPED on
            # its way back to STARTING. Treating that reading as fatal aborted
            # the bootstrap for no reason; re-issuing /start is idempotent, and
            # the attempt count is what still fails a genuinely dead warehouse.
            starts += 1
            if starts > 6:
                raise BootstrapError(
                    f"warehouse fell back to STOPPED after {starts - 1} starts"
                )
            api.call("POST", f"{url}/start", allow=(400,))
        time.sleep(10)
    raise BootstrapError("warehouse did not reach RUNNING within 10 minutes")


def grant_warehouse_use(
    api: Databricks, config: Config, warehouse_id: str, owners: list[str], user: str
) -> None:
    """Let the seeded owners run queries on the demo warehouse.

    Unity Catalog's SELECT is only half of what a second person needs: the
    warehouse carries its own ACL, and "Run queries" comes from CAN USE or
    above. Without this the colleague the demo is built around gets
    `databricks_list_tables` working and `databricks_query_orders` failing --
    a split that looks like a bug in the accelerator rather than a missing
    grant. The deploying user is skipped because creating the warehouse made
    them IS OWNER.
    """
    url = f"{config.host}/api/2.0/permissions/warehouses/{warehouse_id}"
    for owner in owners:
        if owner in {user.lower(), SYNTHETIC_OWNER}:
            continue
        try:
            # PATCH rather than PUT: PUT replaces the whole ACL, so two people
            # bootstrapping the same warehouse would take each other's access
            # away.
            api.call(
                "PATCH",
                url,
                json_body={
                    "access_control_list": [
                        {"user_name": owner, "permission_level": "CAN_USE"}
                    ]
                },
            )
            log(f"granted CAN_USE on the warehouse to {owner}")
        except BootstrapError as error:
            log(f"could not grant warehouse access to {owner}: {error}")


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------


def backticked(name: str) -> str:
    """Quote a principal or identifier for SQL, doubling any backtick in it.

    Every value that reaches this is operator-supplied and runs with the
    operator's own privileges, so nothing crosses a privilege boundary. It is
    here because an unescaped name produces broken SQL, and the failure lands on
    whoever is running the demo for a customer.
    """
    escaped = name.replace("`", "``")
    return f"`{escaped}`"


def run_sql(
    api: Databricks,
    config: Config,
    warehouse_id: str,
    statement: str,
    parameters: list[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "warehouse_id": warehouse_id,
        "statement": statement,
        "wait_timeout": "50s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    }
    if parameters:
        payload["parameters"] = parameters
    body = api.call(
        "POST",
        f"{config.host}/api/2.0/sql/statements",
        json_body=payload,
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


def resolve_owner(api: Databricks, config: Config, address: str) -> str:
    """Return the sign-in name Databricks knows `address` by.

    An address this workspace has never heard of seeds rows that no
    `current_user()` will ever match, and an empty result set is exactly what
    correct filtering looks like -- so the demo would appear to work while
    proving nothing. Better to stop here and name the address.
    """
    if '"' in address or "\\" in address:
        raise BootstrapError(
            f"DATABRICKS_DEMO_OWNERS entry {address!r} is not a sign-in address"
        )
    found = api.call(
        "GET",
        f"{config.host}/api/2.0/preview/scim/v2/Users",
        params={"filter": f'userName eq "{address}"', "count": 1},
        allow=(400, 403),
    )
    if found is None:
        # Listing users is a workspace-admin read. An operator without it can
        # still seed; they just lose this check.
        log(f"cannot look up {address} in SCIM; taking it as configured")
        return address
    for resource in found.get("Resources", []):
        name = str(resource.get("userName") or "")
        if name:
            return name
    raise BootstrapError(
        f"DATABRICKS_DEMO_OWNERS names {address}, who does not exist in this "
        "Databricks workspace. Have them sign in to the workspace once, or "
        "remove them: seeding rows for an address nobody signs in as makes the "
        "isolation demo prove nothing."
    )


def demo_owners(api: Databricks, config: Config, user: str) -> list[str]:
    """Who the seeded orders belong to.

    The bootstrapping user always comes first. `DATABRICKS_DEMO_OWNERS` adds
    further real sign-ins, so two people can run the identical query and get
    different rows. A synthetic owner is always appended so there is a row the
    caller cannot see even when only one real user is seeded.

    Addresses come back lowercased and the row filter lowercases both sides,
    because `Colleague@Contoso.com` typed into an environment variable and
    `colleague@contoso.com` returned by `current_user()` are the same person.
    """
    owners = [user.lower()]
    seen = set(owners)
    for configured in (o.strip() for o in env("DATABRICKS_DEMO_OWNERS").split(",")):
        if not configured:
            continue
        owner = resolve_owner(api, config, configured).lower()
        if owner not in seen:
            owners.append(owner)
            seen.add(owner)
    owners.append(SYNTHETIC_OWNER)
    return owners


def ensure_schema(
    api: Databricks, config: Config, warehouse_id: str, catalog: str, user: str
) -> None:
    """Create the schema the MCP server is configured to read.

    Outside `seed` because `--no-seed` needs it too: the server takes
    DATABRICKS_SCHEMA from the same Bicep output either way, and a deployment
    pointed at a schema that was never created answers every tool call with a
    Unity Catalog error that reads exactly like a permission denial.
    """
    qualified = f"{catalog}.{config.schema}"
    run_sql(api, config, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {qualified}")
    # Unity Catalog privileges are separate from workspace admin: a schema
    # created by one operator cannot be rebuilt by another until ownership
    # moves. Claiming it is harmless when the caller already owns it.
    try:
        run_sql(
            api,
            config,
            warehouse_id,
            f"ALTER SCHEMA {qualified} OWNER TO {backticked(user)}",
        )
    except BootstrapError as error:
        log(f"could not take ownership of {qualified}: {error}")
    log(f"schema ready: {qualified}")


def seed(
    api: Databricks,
    config: Config,
    warehouse_id: str,
    catalog: str,
    user: str,
    owners: list[str],
) -> tuple[int, int]:
    schema = config.schema
    qualified = f"{catalog}.{schema}"
    orders = f"{qualified}.orders"
    row_filter = f"{qualified}.orders_row_filter"
    mask = f"{qualified}.mask_email"

    def sql(statement: str, parameters: list[dict[str, Any]] | None = None) -> None:
        run_sql(api, config, warehouse_id, statement, parameters)

    # Dropping first detaches any existing filter and mask, so the functions can
    # be replaced.
    sql(f"DROP TABLE IF EXISTS {orders}")

    # Both sides are lowercased. `current_user()` returns the sign-in name as
    # Databricks stores it, and an owner typed as `Colleague@Contoso.com` would
    # otherwise match nothing -- which is indistinguishable from the filter
    # working, so the demo would look right and prove nothing.
    sql(
        f"CREATE OR REPLACE FUNCTION {row_filter}(owner STRING) "
        "RETURNS BOOLEAN RETURN lower(owner) = lower(current_user())"
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
        "COMMENT 'Row filtered by lower(owner_email) = lower(current_user())'"
    )

    # Attached before a single row exists. Seeding first left a window in which
    # a transient 5xx, an expired token or a Ctrl-C would leave a populated
    # table with no filter and no mask -- and if a server is already pointed at
    # this schema, every caller then reads every owner's rows with unmasked
    # customer_email, which inverts the one claim this accelerator makes. An
    # ALTER TABLE on an empty table is otherwise identical.
    sql(f"ALTER TABLE {orders} SET ROW FILTER {row_filter} ON (owner_email)")
    sql(f"ALTER TABLE {orders} ALTER COLUMN customer_email SET MASK {mask}")
    log("row filter and column mask attached to an empty table")

    # Three orders per owner, two of them in EU, so the per-user counts the
    # end-to-end harness asserts on hold however many owners are seeded.
    regions = ("EU", "EU", "APAC")
    tuples: list[str] = []
    parameters: list[dict[str, Any]] = []
    for owner in owners:
        for region in regions:
            number = len(tuples) + 1
            row = f"r{number}"
            tuples.append(
                f"(:{row}_id, :{row}_owner, :{row}_region, "
                f":{row}_customer, :{row}_amount)"
            )
            parameters += [
                {"name": f"{row}_id", "type": "STRING", "value": f"ORD-{number:06d}"},
                {"name": f"{row}_owner", "type": "STRING", "value": owner},
                {"name": f"{row}_region", "type": "STRING", "value": region},
                {
                    "name": f"{row}_customer",
                    "type": "STRING",
                    "value": f"customer{number}@northwind.example",
                },
                {
                    "name": f"{row}_amount",
                    "type": "BIGINT",
                    "value": str(10000 + number * 1111),
                },
            ]
    # Bound, not formatted in. `o'brien@contoso.com` is an ordinary sign-in name
    # and possibly the deployer's own, and as a quoted literal it turns the seed
    # into a syntax error. This is the same mechanism `src/databricks.py` uses
    # for the caller's own query.
    try:
        sql(f"INSERT INTO {orders} VALUES {', '.join(tuples)}", parameters)
    except BootstrapError as error:
        # Writing through an attached filter and mask wants a current SQL
        # warehouse channel, and the empty table left behind by a refusal reads
        # as a demo with no data rather than as a failure. Dropping it makes the
        # next run start clean and this run fail loudly.
        #
        # Guarded because the likeliest reasons the insert failed -- an expired
        # token, a warehouse that went away -- take the drop down with it, and
        # an unguarded drop would replace the diagnosis below with its own
        # error. `main` prints the message and no traceback, so a swallowed
        # cause is a cause nobody sees.
        try:
            sql(f"DROP TABLE IF EXISTS {orders}")
        except BootstrapError as cleanup:
            log(f"could not drop the half-seeded {orders}: {cleanup}")
        raise BootstrapError(
            f"could not seed {orders}: {error}\n"
            "  The filter and mask are attached before the insert so no "
            "unprotected rows can ever exist; writing through them needs a "
            "current SQL warehouse channel."
        ) from error
    per_owner = len(regions)
    log(f"seeded {len(tuples)} rows across {len(owners)} owners, {per_owner} each")
    for owner in owners:
        log(f"  owner: {owner}")

    # Grants a non-admin consumer needs. The deployer is usually the catalog
    # owner and already has these implicitly; they are stated so a customer can
    # point the demo at a normal user and have it work unchanged.
    sql(f"GRANT USE CATALOG ON CATALOG {catalog} TO {backticked(user)}")
    sql(f"GRANT USE SCHEMA ON SCHEMA {qualified} TO {backticked(user)}")
    sql(f"GRANT SELECT ON TABLE {orders} TO {backticked(user)}")
    for owner in owners:
        if owner in {user.lower(), SYNTHETIC_OWNER}:
            continue
        # A listed owner who is not provisioned in this workspace cannot be
        # granted, and that should not fail the bootstrap.
        try:
            sql(f"GRANT USE CATALOG ON CATALOG {catalog} TO {backticked(owner)}")
            sql(f"GRANT USE SCHEMA ON SCHEMA {qualified} TO {backticked(owner)}")
            sql(f"GRANT SELECT ON TABLE {orders} TO {backticked(owner)}")
            log(f"granted SELECT on orders to {owner}")
        except BootstrapError as error:
            log(f"could not grant to {owner} (not provisioned?): {error}")
    log("grants applied to orders")
    return len(tuples), per_owner


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
    api = Databricks(credential, config.workspace_resource_id)
    total = per_owner = 0
    try:
        log(f"bootstrapping Unity Catalog on {config.host}")
        user = current_user(api, config)
        log(f"acting as {user}")
        ensure_metastore(api, config)
        catalog = ensure_catalog(api, config)
        warehouse_id = ensure_warehouse(api, config)
        # Resolved before either use, and outside the --no-seed branch: warehouse
        # access is compute, not data, and a colleague pointed at real data needs
        # it just as much as one reading the sample.
        owners = demo_owners(api, config, user)
        grant_warehouse_use(api, config, warehouse_id, owners, user)
        ensure_schema(api, config, warehouse_id, catalog, user)
        if args.no_seed:
            log("--no-seed: leaving the schema contents alone")
        else:
            total, per_owner = seed(api, config, warehouse_id, catalog, user, owners)
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
