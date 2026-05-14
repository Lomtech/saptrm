"""Walk the TRM TOC and download every topic body as JSON."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sap_help_client import SapHelpClient, walk_toc  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "raw"
RAW.mkdir(parents=True, exist_ok=True)
console = Console()

TRM_ROOT = {
    "product_url": "SAP_S4HANA_ON-PREMISE",
    "deliverable_url": "848f8ce21bcd4f67bce77494799e2257",
    "topic_url": "3b3e7e53c4d1cc26e10000000a4450e5.html",
}


async def main(concurrency: int, limit: int | None, force: bool) -> None:
    deliv_dir = RAW / TRM_ROOT["deliverable_url"]
    deliv_dir.mkdir(exist_ok=True)
    topics_dir = deliv_dir / "topics"
    topics_dir.mkdir(exist_ok=True)

    async with SapHelpClient(concurrency=concurrency) as client:
        console.log("Resolving deliverable…")
        h = await client.resolve_deliverable(**TRM_ROOT)
        console.log(f"  deliverable_id={h.deliverable_id}  buildNo={h.build_no}  "
                    f"version={h.version}  lang={h.language}")

        console.log("Fetching full ToC…")
        toc = await client.get_full_toc(h, TRM_ROOT["topic_url"])
        flat = walk_toc(toc)
        console.log(f"  ToC topics: {len(flat)}")

        (deliv_dir / "deliverable.json").write_text(json.dumps({
            "product_url": h.product_url,
            "deliverable_url": h.deliverable_url,
            "deliverable_id": h.deliverable_id,
            "build_no": h.build_no,
            "version": h.version,
            "language": h.language,
            "title": h.title,
        }, indent=2))
        (deliv_dir / "toc.json").write_text(json.dumps(toc, indent=2))
        (deliv_dir / "toc_flat.json").write_text(
            json.dumps([{**t, "path": list(t["path"])} for t in flat], indent=2)
        )

        topics_to_fetch = [t for t in flat if t["file_path"]]
        if limit:
            topics_to_fetch = topics_to_fetch[:limit]

        async def fetch_one(topic: dict, prog: Progress, task_id: int) -> None:
            slug = topic["loio"] or topic["file_path"].rsplit("/", 1)[-1]
            out = topics_dir / f"{slug}.json"
            if out.exists() and not force:
                prog.advance(task_id)
                return
            try:
                page = await client.get_page(h, topic["file_path"], include_toc=False)
            except Exception as e:
                console.log(f"[red]FAIL[/] {slug}: {e}")
                prog.advance(task_id)
                return
            rec = {
                "loio": page["currentPage"].get("loio"),
                "title": page["currentPage"].get("t"),
                "file_path": page["currentPage"].get("u"),
                "page_id": page["currentPage"].get("id"),
                "github_link": page.get("githubLink"),
                "toc_path": list(topic["path"]),
                "depth": topic["depth"],
                "body_html": page.get("body", ""),
            }
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
            prog.advance(task_id)

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
        ) as prog:
            task_id = prog.add_task("Topics", total=len(topics_to_fetch))
            await asyncio.gather(*(fetch_one(t, prog, task_id) for t in topics_to_fetch))

        console.log(f"[green]Done[/]. JSON saved under {topics_dir.relative_to(DATA.parent)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None,
                    help="only fetch first N topics (for testing)")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args()
    asyncio.run(main(args.concurrency, args.limit, args.force))
