"""Akamai-safe backfiller for topics rejected by the httpx scraper.

Uses a real headless Chromium so requests carry full browser fingerprint and
cookies. Replays the same `/http.svc/pagecontent?…` call the Vue-SPA fires,
through the page's own fetch context, so Akamai sees an established session.

Sequential by design — Akamai blocks burst patterns.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

from playwright.async_api import async_playwright
from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DELIVERABLE = "848f8ce21bcd4f67bce77494799e2257"
PRODUCT_URL = "SAP_S4HANA_ON-PREMISE"
BASE = "https://help.sap.com"
console = Console()


async def main(min_delay: float, max_delay: float, limit: int | None) -> None:
    deliv = json.loads((DATA / "raw" / DELIVERABLE / "deliverable.json").read_text())
    flat = json.loads((DATA / "raw" / DELIVERABLE / "toc_flat.json").read_text())
    topics_dir = DATA / "raw" / DELIVERABLE / "topics"
    needed = [t for t in flat if t["file_path"]]
    have = {p.stem for p in topics_dir.glob("*.json")}
    missing = [t for t in needed if (t["loio"] or "") not in have]
    if limit:
        missing = missing[:limit]
    console.log(f"Missing: {len(missing)} (of {len(needed)} needed, {len(have)} have)")

    deliv_id = deliv["deliverable_id"]
    build_no = deliv["build_no"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
        ])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = await ctx.new_page()

        # Warm-up: load the root TRM page so cookies / Akamai session are set.
        warm_url = f"{BASE}/docs/{PRODUCT_URL}/{DELIVERABLE}/{missing[0]['file_path'] if missing else 'index'}"
        console.log(f"Warming up via {warm_url}")
        await page.goto(warm_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        async def fetch_one(file_path: str) -> tuple[int, str | None]:
            url = (f"{BASE}/http.svc/pagecontent"
                   f"?deliverable_id={deliv_id}&buildNo={build_no}"
                   f"&file_path={file_path}")
            # use the page's fetch — inherits cookies + TLS fingerprint
            result = await page.evaluate("""async (url) => {
                const r = await fetch(url, {credentials: 'include',
                                             headers: {'Accept': 'application/json'}});
                return {status: r.status, body: r.status === 200 ? await r.text() : null};
            }""", url)
            return result["status"], result["body"]

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(),
            TextColumn("•"), TimeElapsedColumn(),
            TextColumn("•"), TimeRemainingColumn(),
            console=console,
        ) as prog:
            task = prog.add_task("Backfill", total=len(missing))
            ok = fail = 0
            for t in missing:
                slug = t["loio"] or t["file_path"].rsplit("/", 1)[-1]
                out = topics_dir / f"{slug}.json"
                if out.exists():
                    prog.advance(task)
                    continue

                try:
                    status, body = await fetch_one(t["file_path"])
                except Exception as e:
                    console.log(f"[red]ERR[/] {slug}: {e}")
                    fail += 1
                    prog.advance(task)
                    await asyncio.sleep(random.uniform(min_delay * 2, max_delay * 2))
                    continue

                if status != 200 or not body:
                    console.log(f"[yellow]{status}[/] {slug}")
                    fail += 1
                    prog.advance(task)
                    await asyncio.sleep(random.uniform(min_delay * 2, max_delay * 3))
                    continue

                try:
                    payload = json.loads(body)["data"]
                except Exception as e:
                    console.log(f"[red]parse[/] {slug}: {e}")
                    fail += 1
                    prog.advance(task)
                    continue

                cp = payload["currentPage"]
                rec = {
                    "loio": cp.get("loio"),
                    "title": cp.get("t"),
                    "file_path": cp.get("u"),
                    "page_id": cp.get("id"),
                    "github_link": payload.get("githubLink"),
                    "toc_path": list(t["path"]),
                    "depth": t["depth"],
                    "body_html": payload.get("body", ""),
                }
                out.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
                ok += 1
                prog.advance(task)
                await asyncio.sleep(random.uniform(min_delay, max_delay))

        console.log(f"Done: ok={ok}  fail={fail}")
        await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-delay", type=float, default=0.8)
    ap.add_argument("--max-delay", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.min_delay, args.max_delay, args.limit))
