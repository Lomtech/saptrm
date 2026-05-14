"""Ingest scraped raw topic JSONs into the SQLite knowledge graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser import parse_topic  # noqa: E402
from store import KGStore       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DELIVERABLE = "848f8ce21bcd4f67bce77494799e2257"
console = Console()


def _module_slug(title: str) -> str:
    return title.lower().replace(" ", "_").replace("/", "_")


def ingest(db_path: Path, deliverable: str, *, rebuild: bool) -> None:
    raw_dir = ROOT / "data" / "raw" / deliverable
    topics_dir = raw_dir / "topics"
    if not topics_dir.exists():
        raise SystemExit(f"No topics found at {topics_dir}")
    deliv = json.loads((raw_dir / "deliverable.json").read_text())

    if rebuild and db_path.exists():
        db_path.unlink()

    kg = KGStore(db_path)
    try:
        # ── module nodes from ToC depth-1 children of the root  ────────────
        toc_flat = json.loads((raw_dir / "toc_flat.json").read_text())
        # Top-level "Treasury and Risk Management" is depth=0; its direct
        # children (depth=1) are the major business areas. We treat each
        # depth-1 node as a Module.
        root_title = next((t["title"] for t in toc_flat if t["depth"] == 0), "TRM")
        root_id = kg.upsert_node("module", "trm", root_title, {
            "source": "sap_s4hana_on_prem",
            "deliverable_id": deliv["deliverable_id"],
            "version": deliv["version"],
        })
        modules_by_path: dict[str, int] = {root_title: root_id}
        for t in toc_flat:
            if t["depth"] == 1:
                slug = _module_slug(t["title"])
                mid = kg.upsert_node("module", slug, t["title"], {
                    "loio": t["loio"],
                    "deliverable": deliverable,
                })
                kg.upsert_edge(mid, root_id, "child_of")
                modules_by_path[t["title"]] = mid

        # ── topic nodes & ToC child_of edges  ──────────────────────────────
        topic_files = sorted(topics_dir.glob("*.json"))
        loio_to_node: dict[str, int] = {}
        parsed_cache: dict[str, dict] = {}

        with Progress(console=console) as prog:
            t1 = prog.add_task("Topics", total=len(topic_files))
            for tf in topic_files:
                rec = json.loads(tf.read_text())
                loio = rec["loio"]
                title = rec["title"] or "(untitled)"
                node_id = kg.upsert_node("topic", loio, title, {
                    "file_path": rec["file_path"],
                    "deliverable": deliverable,
                })
                loio_to_node[loio] = node_id

                # Parse body
                parsed = parse_topic(loio=loio, title=title,
                                     body_html=rec.get("body_html") or "")
                parsed_cache[loio] = {"parsed": parsed, "rec": rec, "node_id": node_id}

                kg.upsert_topic_body(
                    node_id,
                    loio=loio,
                    file_path=rec["file_path"],
                    page_id=rec.get("page_id"),
                    topic_type=parsed.topic_type,
                    depth=rec["depth"],
                    toc_path=rec["toc_path"],
                    github_link=rec.get("github_link"),
                    body_html=rec.get("body_html") or "",
                    body_text=parsed.body_text,
                    short_desc=parsed.short_desc,
                )

                # ToC parent edge — first ancestor that is also a topic.
                toc_path = rec["toc_path"]
                if len(toc_path) >= 2:
                    parent_title = toc_path[-2]
                    if parent_title in modules_by_path:
                        kg.upsert_edge(node_id, modules_by_path[parent_title], "child_of")
                    else:
                        # parent is itself a topic — resolve via title match later
                        pass

                prog.advance(t1)

        # Resolve topic→topic child_of by title (depth-1 already covered above)
        title_to_loio = {p["parsed"].title: loio for loio, p in parsed_cache.items()}
        for loio, payload in parsed_cache.items():
            rec = payload["rec"]
            toc_path = rec["toc_path"]
            if len(toc_path) < 2:
                continue
            parent_title = toc_path[-2]
            if parent_title in modules_by_path:
                continue  # already linked to a module
            parent_loio = title_to_loio.get(parent_title)
            if parent_loio and parent_loio in loio_to_node:
                kg.upsert_edge(payload["node_id"], loio_to_node[parent_loio], "child_of")

        # ── mention / uses_table / uses_transaction / calls_function ───────
        for loio, payload in parsed_cache.items():
            parsed = payload["parsed"]
            src_id = payload["node_id"]
            # internal links → 'mentions' edges
            for anchor, target_loio in parsed.internal_links:
                dst = loio_to_node.get(target_loio)
                if dst is None:
                    kg.record_unresolved_link(loio, target_loio + ".html", anchor, "out-of-deliverable")
                    continue
                kg.upsert_edge(src_id, dst, "mentions", weight=1.0,
                               props={"anchor": anchor[:120]})
            # transactions
            for code in parsed.tx_codes:
                tx_id = kg.upsert_node("transaction", code, code)
                kg.upsert_edge(src_id, tx_id, "uses_transaction")
            # tables
            for tbl in parsed.tables:
                tb_id = kg.upsert_node("table", tbl, tbl)
                kg.upsert_edge(src_id, tb_id, "uses_table")
            # function modules / BAPIs
            for fm in parsed.function_modules:
                fm_id = kg.upsert_node("function", fm, fm)
                kg.upsert_edge(src_id, fm_id, "calls_function")

        # ── module membership for tx/table/function ────────────────────────
        # If a topic uses_transaction X and is a descendant of module M,
        # then X belongs_to_module M (weighted by number of co-occurrences).
        # Same for tables and functions.
        for edge_type, target_type in [
            ("uses_transaction", "transaction"),
            ("uses_table", "table"),
            ("calls_function", "function"),
        ]:
            for row in kg.conn.execute(
                f"""SELECT e.dst_id, m.id AS module_id
                    FROM edges e
                    JOIN topic_bodies tb ON tb.node_id = e.src_id
                    JOIN nodes m
                      ON m.type='module'
                     AND m.title = COALESCE(
                         json_extract(tb.toc_path_json, '$[1]'),
                         json_extract(tb.toc_path_json, '$[0]')
                     )
                    WHERE e.type=?""",
                (edge_type,),
            ).fetchall():
                kg.upsert_edge(row["dst_id"], row["module_id"], "belongs_to_module", weight=0.25)

        kg.conn.commit()
        console.log("Rebuilding FTS index…")
        kg.rebuild_fts()
        kg.conn.commit()

        stats = kg.stats()
        console.print(stats)
    finally:
        kg.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "graph" / "trm.sqlite"))
    ap.add_argument("--deliverable", default=DEFAULT_DELIVERABLE)
    ap.add_argument("--rebuild", action="store_true",
                    help="drop existing DB before loading")
    args = ap.parse_args()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ingest(db_path, args.deliverable, rebuild=args.rebuild)
