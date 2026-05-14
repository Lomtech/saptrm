"""Thin sqlite wrapper around the SAP TRM knowledge graph.

Single-writer model; readers can open the same file concurrently because of WAL.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


@dataclass(frozen=True)
class Node:
    id: int
    type: str
    slug: str
    title: str
    props: dict[str, Any]


@dataclass(frozen=True)
class Edge:
    src_id: int
    dst_id: int
    type: str
    weight: float
    props: dict[str, Any]


class KGStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_PATH.read_text())

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- mutators ------------------------------------------------------

    def upsert_node(self, type: str, slug: str, title: str,
                    props: dict[str, Any] | None = None) -> int:
        props_json = json.dumps(props or {}, ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT INTO nodes(type, slug, title, props_json) VALUES (?,?,?,?) "
            "ON CONFLICT(type, slug) DO UPDATE SET title=excluded.title, "
            "props_json=excluded.props_json RETURNING id",
            (type, slug, title, props_json),
        )
        return int(cur.fetchone()[0])

    def upsert_edge(self, src_id: int, dst_id: int, type: str,
                    weight: float = 1.0, props: dict[str, Any] | None = None) -> None:
        props_json = json.dumps(props or {}, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO edges(src_id, dst_id, type, weight, props_json) VALUES (?,?,?,?,?) "
            "ON CONFLICT(src_id, dst_id, type) DO UPDATE SET "
            "weight = edges.weight + excluded.weight, props_json = excluded.props_json",
            (src_id, dst_id, type, weight, props_json),
        )

    def upsert_topic_body(self, node_id: int, *, loio: str, file_path: str,
                          page_id: int | None, topic_type: str, depth: int,
                          toc_path: list[str], github_link: str | None,
                          body_html: str, body_text: str, short_desc: str | None) -> None:
        self.conn.execute(
            "INSERT INTO topic_bodies(node_id, loio, file_path, page_id, topic_type, "
            "depth, toc_path_json, github_link, body_html, body_text, short_desc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET "
            " loio=excluded.loio, file_path=excluded.file_path, page_id=excluded.page_id,"
            " topic_type=excluded.topic_type, depth=excluded.depth,"
            " toc_path_json=excluded.toc_path_json, github_link=excluded.github_link,"
            " body_html=excluded.body_html, body_text=excluded.body_text,"
            " short_desc=excluded.short_desc",
            (node_id, loio, file_path, page_id, topic_type, depth,
             json.dumps(toc_path, ensure_ascii=False), github_link,
             body_html, body_text, short_desc),
        )

    def record_unresolved_link(self, src_loio: str, href: str, anchor: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO unresolved_links(src_topic_loio, href, anchor_text, reason) VALUES (?,?,?,?)",
            (src_loio, href, anchor, reason),
        )

    # ---- fts -----------------------------------------------------------

    def rebuild_fts(self) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM topics_fts")
            c.execute("""
                INSERT INTO topics_fts(title, body_text, toc_path, short_desc, loio, node_id)
                SELECT n.title, tb.body_text, tb.toc_path_json, COALESCE(tb.short_desc, ''),
                       tb.loio, n.id
                FROM topic_bodies tb
                JOIN nodes n ON n.id = tb.node_id
            """)

    # ---- reads (used by MCP tools) -------------------------------------

    def search_topics(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT loio, node_id, title, snippet(topics_fts, 1, '<<','>>', '…', 24) AS snippet,
                      bm25(topics_fts) AS rank
               FROM topics_fts
               WHERE topics_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_topic(self, loio: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT n.id AS node_id, n.title, tb.loio, tb.file_path, tb.topic_type,
                      tb.depth, tb.toc_path_json, tb.github_link, tb.body_text, tb.short_desc
               FROM topic_bodies tb JOIN nodes n ON n.id = tb.node_id
               WHERE tb.loio = ?""",
            (loio,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["toc_path"] = json.loads(d.pop("toc_path_json"))
        return d

    def neighbors(self, node_id: int, edge_types: list[str] | None = None,
                  direction: str = "both", limit: int = 50) -> list[dict[str, Any]]:
        et_clause = ""
        et_params: list[Any] = []
        if edge_types:
            et_clause = f" AND e.type IN ({','.join('?' * len(edge_types))})"
            et_params = list(edge_types)

        parts: list[str] = []
        params: list[Any] = []
        if direction in ("out", "both"):
            parts.append(
                "SELECT e.type AS edge, 'out' AS direction, e.weight, "
                "n.id AS node_id, n.type AS node_type, n.slug, n.title "
                "FROM edges e JOIN nodes n ON n.id = e.dst_id "
                f"WHERE e.src_id = ?{et_clause}"
            )
            params += [node_id, *et_params]
        if direction in ("in", "both"):
            parts.append(
                "SELECT e.type AS edge, 'in' AS direction, e.weight, "
                "n.id AS node_id, n.type AS node_type, n.slug, n.title "
                "FROM edges e JOIN nodes n ON n.id = e.src_id "
                f"WHERE e.dst_id = ?{et_clause}"
            )
            params += [node_id, *et_params]
        sql = " UNION ALL ".join(parts) + " ORDER BY weight DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_node(self, type: str, slug: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, type, slug, title, props_json FROM nodes WHERE type=? AND slug=?",
            (type, slug),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["props"] = json.loads(d.pop("props_json"))
        return d

    def find_nodes_by_title(self, q: str, type_: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        if type_:
            rows = self.conn.execute(
                "SELECT id, type, slug, title FROM nodes WHERE type=? AND title LIKE ? LIMIT ?",
                (type_, f"%{q}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, type, slug, title FROM nodes WHERE title LIKE ? LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def shortest_path(self, src_id: int, dst_id: int, max_depth: int = 5) -> list[dict[str, Any]] | None:
        """BFS over edges (undirected). Returns the sequence of nodes."""
        if src_id == dst_id:
            row = self.conn.execute("SELECT id, type, slug, title FROM nodes WHERE id=?", (src_id,)).fetchone()
            return [dict(row)] if row else None

        visited = {src_id: None}
        frontier = [src_id]
        for _depth in range(max_depth):
            next_frontier: list[int] = []
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT src_id, dst_id FROM edges WHERE src_id IN ({placeholders}) "
                f"UNION SELECT dst_id, src_id FROM edges WHERE dst_id IN ({placeholders})",
                (*frontier, *frontier),
            ).fetchall()
            for src, dst in rows:
                if dst in visited:
                    continue
                visited[dst] = src
                if dst == dst_id:
                    # reconstruct
                    path_ids = [dst_id]
                    cur = dst_id
                    while visited[cur] is not None:
                        cur = visited[cur]
                        path_ids.append(cur)
                    path_ids.reverse()
                    placeholders2 = ",".join("?" * len(path_ids))
                    nodes = self.conn.execute(
                        f"SELECT id, type, slug, title FROM nodes WHERE id IN ({placeholders2})",
                        path_ids,
                    ).fetchall()
                    by_id = {r["id"]: dict(r) for r in nodes}
                    return [by_id[i] for i in path_ids]
                next_frontier.append(dst)
            frontier = next_frontier
        return None

    def stats(self) -> dict[str, Any]:
        n_total = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        e_total = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        by_node_type = dict(self.conn.execute(
            "SELECT type, COUNT(*) c FROM nodes GROUP BY type"
        ).fetchall())
        by_edge_type = dict(self.conn.execute(
            "SELECT type, COUNT(*) c FROM edges GROUP BY type"
        ).fetchall())
        return {"nodes_total": n_total, "edges_total": e_total,
                "nodes_by_type": by_node_type, "edges_by_type": by_edge_type}
