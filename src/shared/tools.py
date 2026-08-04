"""The tool names, which are a contract between the two workloads.

The chat app passes these to the agent as its allow-list, so a name that drifts
from what the server registers does not fail loudly -- the tool simply stops
being callable, and the agent says it cannot do the thing. Declaring the names
in one place both sides import removes half of that risk; `test_contracts.py`
removes the other half by asserting that a registered server offers exactly the
set named here.
"""

from __future__ import annotations

ORDER_TOOLS = frozenset(
    {"who_am_i", "list_orders", "get_order", "create_order", "update_order_status"}
)

DATABRICKS_TOOLS = frozenset(
    {"databricks_whoami", "databricks_list_tables", "databricks_query_orders"}
)
