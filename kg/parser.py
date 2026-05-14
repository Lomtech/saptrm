"""Parse SAP Help DITA-rendered HTML into entities + relations.

What we extract per topic:
    - topic_type        from CSS class (concept|task|reference|unknown)
    - title             from <title>
    - short_desc        from <meta name="abstract"> or <meta name="description">
    - body_text         clean plain text for FTS / embeddings
    - internal_links    list of (anchor_text, target_loio)
    - tx_codes          mentions of SAP transaction codes
    - table_names       mentions of SAP DB tables  (whitelist-driven)
    - function_modules  mentions of SAP function modules / BAPIs

The patterns are deliberately conservative — false positives in a knowledge
graph are worse than missing edges. Whitelists in
`SAP_TRM_TABLES` / `SAP_TRM_TXCODES` ground the extraction; the regexes are
fall-backs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from selectolax.parser import HTMLParser

# ─── well-known SAP TRM artefacts ─────────────────────────────────────────────
# (Seed lists; the graph grows as more topics are parsed.)

SAP_TRM_TABLES: set[str] = {
    # Master data
    "VTBFHA", "VTBFHAPO", "VTVDIR", "VTBPF", "VTBNF", "VTIDER", "VTIDERIV",
    "VTIPF", "VTIVA", "TZB0T", "TZPA", "TZB13", "TZB02", "TZP02", "TZPAT",
    # Position management
    "TPM_HM", "TPM_VAL_AREA", "TPM_TRG", "VTBKEY", "VTBZAHL",
    # Hedge management
    "JBRAA", "JBRGAZSCD", "FTI_TR_PERIOD",
    # Market data
    "TCURR", "TCURX", "TCURF", "TCURV", "TCURT",
    # FI integration (cross-module but referenced often)
    "BSEG", "BKPF", "BSID", "BSIK", "BSAD", "BSAK",
    # Bank account mgmt
    "T012", "T012K", "BNKA",
}

SAP_TRM_TX_PREFIXES: tuple[str, ...] = ("TPM", "TBB", "TBC", "FTR", "FF", "JBR", "OT", "F9")

# Anchored at word boundaries; require at least one digit so that "INFO"/"END"
# don't get matched.
RE_TXCODE = re.compile(r"\b([A-Z]{2,4}[0-9][A-Z0-9]{0,4})\b")
# `transaction <CODE>` and `(<CODE>)` are strong signals
RE_TXCODE_HINT = re.compile(r"(?i)(?:transaction|tcode|t-code)\s+([A-Z]{2,4}[0-9][A-Z0-9]{0,4})\b")

# BAPIs and function modules. Conservative: must start with a known prefix.
RE_FM = re.compile(r"\b((?:BAPI|FTR|TR|TPM|FM|FI|JBR)_[A-Z0-9_]{2,60})\b")

# Internal SAP Help links use href like "<loio>.html" (32 hex chars + .html).
RE_INTERNAL_LOIO = re.compile(r"^([0-9a-f]{32})\.html(?:#.*)?$")

# DITA topic-type classes that may appear on the root <article> / wrapper <div>
TOPIC_TYPE_CLASSES = ("concept", "task", "reference", "glossentry",
                       "topic", "troubleshooting", "learning")


@dataclass
class ParsedTopic:
    loio: str
    title: str
    topic_type: str
    short_desc: str | None
    body_text: str
    internal_links: list[tuple[str, str]] = field(default_factory=list)  # (anchor, target_loio)
    tx_codes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    function_modules: list[str] = field(default_factory=list)
    unresolved_links: list[tuple[str, str]] = field(default_factory=list)  # (anchor, raw_href)


def _detect_topic_type(tree: HTMLParser) -> str:
    """Look at the first wrapper div's classes for a DITA topic-type."""
    # Most SAP Help bodies wrap their payload in <div class="concept …"> etc.
    for node in tree.css("body > div, body > article, div[class]"):
        classes = (node.attributes.get("class") or "").split()
        for c in classes:
            if c in TOPIC_TYPE_CLASSES:
                return c
        # the duplicated form: "concept concept-concept"
        for c in classes:
            if c.endswith("-concept"):
                return "concept"
            if c.endswith("-task"):
                return "task"
            if c.endswith("-reference"):
                return "reference"
        break
    return "unknown"


def _meta(tree: HTMLParser, name: str) -> str | None:
    n = tree.css_first(f'meta[name="{name}"]')
    if not n:
        return None
    return n.attributes.get("content") or None


def _clean_text(tree: HTMLParser) -> str:
    body = tree.css_first("body") or tree
    # drop script/style/nav noise
    for sel in ("script", "style", "nav", "header", "footer"):
        for n in body.css(sel):
            n.decompose()
    txt = body.text(separator=" ", strip=True)
    # collapse whitespace
    return re.sub(r"\s+", " ", txt).strip()


def parse_topic(*, loio: str, title: str, body_html: str) -> ParsedTopic:
    tree = HTMLParser(body_html)
    topic_type = _detect_topic_type(tree)
    short_desc = _meta(tree, "abstract") or _meta(tree, "description")
    body_text = _clean_text(tree)

    # internal links (.html anchors → loios)
    internal: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        anchor = (a.text(strip=True) or "")[:200]
        if not href or href.startswith(("http://", "https://", "mailto:", "javascript:")):
            continue
        m = RE_INTERNAL_LOIO.match(href)
        if m:
            target_loio = m.group(1)
            if target_loio != loio:
                internal.append((anchor, target_loio))
        else:
            unresolved.append((anchor, href))

    # Tx-codes: collect from hint regex, then a wider sweep filtered by prefix.
    tx_set: set[str] = set()
    for m in RE_TXCODE_HINT.finditer(body_text):
        tx_set.add(m.group(1).upper())
    for m in RE_TXCODE.finditer(body_text):
        code = m.group(1).upper()
        if any(code.startswith(p) for p in SAP_TRM_TX_PREFIXES):
            tx_set.add(code)

    # Tables: only count whitelisted tables that appear as standalone words.
    tables_found: set[str] = set()
    upper_tokens = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,9}\b", body_text))
    tables_found.update(upper_tokens & SAP_TRM_TABLES)

    # FMs / BAPIs
    fms: set[str] = set()
    for m in RE_FM.finditer(body_text):
        fms.add(m.group(1).upper())

    # de-dup, deterministic order
    return ParsedTopic(
        loio=loio,
        title=title,
        topic_type=topic_type,
        short_desc=short_desc,
        body_text=body_text,
        internal_links=sorted(set(internal)),
        tx_codes=sorted(tx_set),
        tables=sorted(tables_found),
        function_modules=sorted(fms),
        unresolved_links=unresolved,
    )
