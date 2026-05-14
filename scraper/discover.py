"""Sniff SAP Help Portal network traffic to find the real content/TOC endpoints."""
import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT_URL = "https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/848f8ce21bcd4f67bce77494799e2257/3b3e7e53c4d1cc26e10000000a4450e5.html"
OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "discovery.jsonl"


async def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    interesting: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "help.sap.com" not in url:
                return
            ct = (resp.headers or {}).get("content-type", "")
            if not (ct.startswith("application/json") or "json" in ct):
                return
            try:
                body = await resp.body()
            except Exception:
                return
            # Skip huge bodies in summary log
            preview = body[:2000].decode("utf-8", errors="replace")
            interesting.append({
                "url": url,
                "status": resp.status,
                "content_type": ct,
                "size": len(body),
                "preview": preview,
            })
            print(f"[{resp.status}] {len(body):>8}B  {url}")

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"→ navigating: {ROOT_URL}")
        await page.goto(ROOT_URL, wait_until="domcontentloaded", timeout=60000)
        # let xhr settle
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        # extra time for late lazy chunks
        await page.wait_for_timeout(3000)

        title = await page.title()
        print("page title:", title)

        # try to capture some on-page DOM signals
        try:
            await page.wait_for_selector("#topic", timeout=5000)
            h1 = await page.text_content("#topic h1") or ""
        except Exception:
            h1 = ""
        print("topic h1:", h1[:120])

        await browser.close()

    with OUT.open("w") as f:
        for entry in interesting:
            f.write(json.dumps(entry) + "\n")
    print(f"\nSaved {len(interesting)} JSON responses → {OUT}")

    # quick endpoint summary
    paths: dict[str, int] = {}
    for e in interesting:
        m = re.match(r"https?://[^/]+(/[^?#]+)", e["url"])
        p_ = m.group(1) if m else e["url"]
        paths[p_] = paths.get(p_, 0) + 1
    print("\nEndpoint frequency:")
    for p_, c in sorted(paths.items(), key=lambda x: -x[1]):
        print(f"  {c:>3}x  {p_}")


if __name__ == "__main__":
    asyncio.run(main())
