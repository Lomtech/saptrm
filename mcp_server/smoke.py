"""Direct in-process smoke test of the MCP tool surface — no transport.

We call the underlying functions instead of going through stdio because
that's faster to iterate on and proves the tool logic is correct.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# import the module so that the `_kg` singleton initialises
import server  # noqa: E402

def dump(name: str, value):
    print(f"\n──── {name} ────")
    print(json.dumps(value, indent=2, default=str)[:1800])


dump("kg_stats", server.kg_stats())

dump("kg_module_index", server.kg_module_index())

# Find Transaction Manager module then list its first ToC children
nodes = server.kg_find_nodes("Transaction Manager", node_type="module", limit=3)
dump("find 'Transaction Manager'", nodes)
if nodes:
    tm = nodes[0]
    dump("Transaction Manager .neighbors (child_of, in)",
         server.kg_neighbors(tm["id"], edge_types=["child_of"], direction="in", limit=10))

# Look up a transaction and ask the graph what mentions it
tx = server.kg_find_nodes("FW17", node_type="transaction")
dump("find 'FW17'", tx)
if tx:
    dump("FW17 neighbors (in)",
         server.kg_neighbors(tx[0]["id"], direction="in", limit=10))

# Full-text search test
dump("search 'hedge accounting position'",
     server.kg_search('"hedge accounting" position', limit=5))

# Topic detail
hits = server.kg_search('hedge management', limit=1)
if hits:
    dump("kg_topic(first hit)", server.kg_topic(hits[0]["loio"], max_body_chars=600))
    dump("kg_node_summary", server.kg_node_summary(hits[0]["node_id"]))
