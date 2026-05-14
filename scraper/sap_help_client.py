"""Async client for the SAP Help Portal (Vue-SPA) internal JSON endpoints.

Discovered via Playwright network sniff:
  /http.svc/deliverableMetadata?product_url=...&deliverable_url=...&topic_url=...
      → numeric deliverable_id + buildNo
  /http.svc/pagecontent?deliverable_id=...&buildNo=...&file_path=...&deliverableInfo=1
      → currentPage (loio,id,t,u) + body (HTML) + deliverable.fullToc (tree)

deliverableInfo=1 ⇒ include fullToc and deliverable metadata in the response.
Subsequent per-topic calls can omit deliverableInfo to keep payloads small.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

BASE = "https://help.sap.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://help.sap.com/",
}


@dataclass(frozen=True)
class DeliverableHandle:
    product_url: str          # e.g. "SAP_S4HANA_ON-PREMISE"
    deliverable_url: str      # e.g. "848f8ce21bcd4f67bce77494799e2257" (loio of deliverable)
    deliverable_id: int       # numeric internal id, e.g. 40374870
    build_no: int             # e.g. 1779
    title: str
    version: str
    language: str


class SapHelpClient:
    def __init__(self, *, concurrency: int = 4, language: str = "en-US",
                 state: str = "PRODUCTION", timeout_s: float = 30.0):
        self._sem = asyncio.Semaphore(concurrency)
        self.language = language
        self.state = state
        self._client = httpx.AsyncClient(
            base_url=BASE,
            headers=DEFAULT_HEADERS,
            timeout=timeout_s,
            follow_redirects=True,
        )

    async def __aenter__(self) -> "SapHelpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(4),
           wait=wait_exponential(multiplier=1, min=1, max=15))
    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._sem:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            data = r.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"SAP Help non-OK: {data}")
        return data["data"]

    async def resolve_deliverable(
        self, product_url: str, deliverable_url: str, topic_url: str,
        *, version: str = "LATEST",
    ) -> DeliverableHandle:
        d = await self._get_json("/http.svc/deliverableMetadata", {
            "product_url": product_url,
            "topic_url": topic_url,
            "version": version,
            "deliverable_url": deliverable_url,
            "deliverableInfo": 1,
            "toc": 1,
        })
        deliv = d["deliverable"]
        return DeliverableHandle(
            product_url=product_url,
            deliverable_url=deliverable_url,
            deliverable_id=int(deliv["id"]),
            build_no=int(deliv["buildNo"]),
            title=deliv.get("productName", "") + " · " + d.get("filePath", ""),
            version=deliv.get("version", ""),
            language=deliv.get("language", self.language),
        )

    async def get_page(
        self, h: DeliverableHandle, file_path: str, *, include_toc: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "deliverable_id": h.deliverable_id,
            "buildNo": h.build_no,
            "file_path": file_path,
        }
        if include_toc:
            params["deliverableInfo"] = 1
        return await self._get_json("/http.svc/pagecontent", params)

    async def get_full_toc(self, h: DeliverableHandle, first_file_path: str) -> list[dict[str, Any]]:
        data = await self.get_page(h, first_file_path, include_toc=True)
        return data["deliverable"]["fullToc"]


def walk_toc(nodes: list[dict[str, Any]], parent_path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Depth-first flatten of the ToC tree. Each item gains 'path' (titles)
    and 'depth'."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        title = n.get("t") or ""
        url = n.get("u") or ""
        path = parent_path + (title,)
        out.append({
            "loio": (url[:-5] if url.endswith(".html") else url) or None,
            "title": title,
            "file_path": url,
            "id": int(n["id"]) if n.get("id") else None,
            "depth": len(parent_path),
            "path": path,
        })
        kids = n.get("c") or []
        if kids:
            out.extend(walk_toc(kids, path))
    return out
