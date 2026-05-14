"""SAP TRM Knowledge-Graph MCP server.

Exposes the TRM Help Portal corpus as queryable tools:

  kg_search          full-text search across topics (BM25-ranked)
  kg_topic           full topic record (title, type, body_text, toc path)
  kg_neighbors       graph neighbours of a node (in/out/both)
  kg_find_nodes      look up nodes of any type by partial title / slug
  kg_shortest_path   BFS shortest path between two nodes
  kg_module_index    list modules + topic counts
  kg_node_summary    one-shot view of a node + its top neighbours
  kg_stats           overall corpus counts
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "kg"))
from store import KGStore  # noqa: E402

DB_PATH = Path(os.environ.get("SAP_TRM_KG_DB", ROOT / "data" / "graph" / "trm.sqlite"))

mcp = FastMCP("sap-trm-kg")
_kg = KGStore(DB_PATH)


def _truncate(s: str | None, limit: int) -> str:
    if s is None:
        return ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


@mcp.tool()
def kg_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Full-text search across all TRM topics (BM25-ranked).

    `query` uses FTS5 syntax: bare terms, "exact phrases", AND/OR/NOT,
    or column filters like `title: hedge`.
    """
    hits = _kg.search_topics(query, limit=limit)
    out = []
    for h in hits:
        out.append({
            "loio": h["loio"],
            "node_id": h["node_id"],
            "title": h["title"],
            "snippet": h["snippet"],
            "rank": h["rank"],
        })
    return out


@mcp.tool()
def kg_topic(loio: str, *, max_body_chars: int = 6000) -> dict[str, Any] | None:
    """Return the full record of a topic by its loio (the 32-hex SAP id).

    `max_body_chars` truncates `body_text` to keep responses manageable.
    Set very large or 0 to disable truncation (0 ⇒ unlimited).
    """
    t = _kg.get_topic(loio)
    if not t:
        return None
    if max_body_chars and len(t["body_text"]) > max_body_chars:
        t["body_text"] = t["body_text"][:max_body_chars] + " …(truncated)"
    return t


@mcp.tool()
def kg_neighbors(node_id: int, *, edge_types: list[str] | None = None,
                 direction: str = "both", limit: int = 30) -> list[dict[str, Any]]:
    """List graph neighbours of `node_id`.

    `edge_types` filters by edge label (`child_of`, `mentions`,
    `uses_transaction`, `uses_table`, `calls_function`, `belongs_to_module`).
    `direction` ∈ {`in`, `out`, `both`}.
    """
    return _kg.neighbors(node_id, edge_types=edge_types,
                         direction=direction, limit=limit)


@mcp.tool()
def kg_find_nodes(query: str, *, node_type: str | None = None,
                  limit: int = 25) -> list[dict[str, Any]]:
    """Find nodes by partial title or slug match. Useful to locate the
    canonical `node_id` for a transaction code, BAPI name, table name,
    or module title before calling `kg_neighbors`.

    `node_type` ∈ {`topic`, `module`, `transaction`, `table`, `function`,
    `term`, `img`}. None = all.
    """
    return _kg.find_nodes_by_title(query, type_=node_type, limit=limit)


@mcp.tool()
def kg_shortest_path(src_node_id: int, dst_node_id: int,
                     max_depth: int = 5) -> list[dict[str, Any]] | None:
    """BFS shortest (undirected) path between two nodes. Useful to find
    how a transaction relates to a topic, or two topics relate via mentions.
    Returns `None` if no path within `max_depth`.
    """
    return _kg.shortest_path(src_node_id, dst_node_id, max_depth=max_depth)


@mcp.tool()
def kg_module_index() -> list[dict[str, Any]]:
    """List all TRM modules with their direct child counts."""
    rows = _kg.conn.execute("""
        SELECT n.id, n.slug, n.title,
               (SELECT COUNT(*) FROM edges e
                  JOIN nodes c ON c.id = e.src_id
                  WHERE e.dst_id = n.id AND e.type='child_of') AS direct_children
        FROM nodes n WHERE n.type='module'
        ORDER BY n.title
    """).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def kg_node_summary(node_id: int) -> dict[str, Any] | None:
    """One-shot overview of a node: its title, type, top mentions out,
    top mentioned-by (in), parent module if any, and short_desc for topics.
    """
    row = _kg.conn.execute(
        "SELECT id, type, slug, title, props_json FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if not row:
        return None
    out: dict[str, Any] = dict(row)
    out["props"] = json.loads(out.pop("props_json"))

    if out["type"] == "topic":
        body = _kg.conn.execute(
            "SELECT topic_type, depth, short_desc, toc_path_json, loio FROM topic_bodies WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if body:
            out["topic_type"] = body["topic_type"]
            out["depth"] = body["depth"]
            out["short_desc"] = body["short_desc"]
            out["toc_path"] = json.loads(body["toc_path_json"])
            out["loio"] = body["loio"]

    out["neighbors_out"] = _kg.neighbors(node_id, direction="out", limit=15)
    out["neighbors_in"]  = _kg.neighbors(node_id, direction="in",  limit=15)
    return out


@mcp.tool()
def kg_stats() -> dict[str, Any]:
    """Counts of nodes and edges by type. Use as a sanity check."""
    return _kg.stats()


if __name__ == "__main__":
    mcp.run()
