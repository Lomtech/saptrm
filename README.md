# SAP TRM Consultant Agent

Lokaler "ultimativer SAP S/4HANA Treasury & Risk Management Consultant" für Claude Code.
Speist sich aus einem Knowledge Graph der offiziellen SAP-Help-TRM-Dokumentation
(S/4HANA on-prem, Version 2025 FPS01, Deliverable `848f8ce21bcd4f67bce77494799e2257`).

## Was steht jetzt

```
sap-trm-agent/
├── scraper/
│   ├── discover.py              # Playwright-Sniffer der SAP-Help-Endpoints (Doku-Tool)
│   ├── sap_help_client.py       # async httpx-Client für /http.svc/pagecontent + deliverableMetadata
│   ├── crawl.py                 # ToC-Walker, schreibt data/raw/<deliv>/topics/<loio>.json
│   └── backfill_playwright.py   # Akamai-safe Backfiller mit echtem Chromium
├── kg/
│   ├── schema.sql               # nodes / edges / topic_bodies / topics_fts (SQLite + FTS5)
│   ├── store.py                 # KGStore: upsert, neighbors, search, shortest_path, stats
│   ├── parser.py                # DITA-HTML → entities (Tx, BAPI, Table) + Edges
│   └── ingest.py                # raw/*.json → SQLite KG
├── mcp_server/
│   ├── server.py                # FastMCP-Server "sap-trm-kg" (stdio)
│   ├── smoke.py                 # In-Process-Smoketest der Tool-Logik
│   └── stdio_test.py            # End-to-end JSON-RPC-Test
└── data/
    ├── raw/<deliv>/
    │   ├── deliverable.json     # numerische deliverable_id + buildNo + version
    │   ├── toc.json             # roher ToC-Baum von SAP
    │   ├── toc_flat.json        # flach abgewickelt, mit toc_path
    │   └── topics/<loio>.json   # 891 / 1589 — Body-HTML + ToC-Pfad pro Topic
    └── graph/trm.sqlite         # der KG (1083 Nodes, 2800 Edges, FTS5)
```

## Stand des Korpus

```
nodes:  1083    edges:  2800
  topic        891   (von 1589 ToC-Einträgen — 698 fehlen wegen Akamai-Rate-Limit)
  module        19   (Transaction Manager, Hedge Mgmt of FX Risks, Risk Mgmt,
                      Exposure Mgmt 2.0, Market Data Mgmt, Master Data,
                      Treasury Analytics, …)
  transaction  102   (TPM*, TBB*, JBR*, FW*, OTC*, FTREX*, F9*, …)
  function      68   (BAPI_FTR_*, BAPI_TEX_*, BAPI_THA_*, BAPI_TEM_*, …)
  table          3   (whitelist-getrieben — siehe parser.py)

  child_of        887
  mentions       1408
  uses_transaction 167
  calls_function   115
  belongs_to_module 216
  uses_table         7
```

## Verwendung — als Claude-Code-Agent

1. **Restart Claude Code**, damit die neue Agent-Definition + der MCP-Server
   aufgegriffen werden.
2. In jeder Session frag z. B.:
   - *"Was macht Transaction FTREX15?"*
   - *"Welche BAPIs gibt es im Hedge Management of FX Risks?"*
   - *"Erklär mir den Unterschied zwischen Hedge Accounting for Exposures (E-HA) und Hedge Management of FX Risks."*
   - *"Wo ist im Customizing der Exposure-Position-Transfer konfiguriert?"*

   Claude Code aktiviert automatisch den `sap-trm-consultant`-Subagent (siehe
   `~/.claude/agents/sap-trm-consultant.md`), der dann `mcp__sap-trm-kg__*`
   für Suche + Graph-Navigation nutzt und alle Aussagen mit SAP-Help-URLs belegt.

3. Direkter Tool-Aufruf vom Haupt-Claude geht auch, z. B.:
   `mcp__sap-trm-kg__kg_search`, `kg_find_nodes`, `kg_neighbors`,
   `kg_topic`, `kg_node_summary`, `kg_module_index`, `kg_shortest_path`,
   `kg_stats`.

## MCP-Server-Registration (bereits eingetragen)

```sh
claude mcp add --scope user sap-trm-kg \
  /Users/lom/Developer/sap-trm-agent/.venv/bin/python \
  /Users/lom/Developer/sap-trm-agent/mcp_server/server.py
```

`claude mcp list` muss zeigen: `sap-trm-kg — ✓ Connected`.

## Die fehlenden 698 Topics nachholen

Akamai blockt momentan unsere IP für alle `pagecontent`-Calls — egal ob
`httpx`, echtes Chromium, mit oder ohne Cookies. Erfahrungswert: das Block
löst sich nach 30–60 min Cooldown wieder. So holst du den Rest nach:

```sh
cd /Users/lom/Developer/sap-trm-agent

# Variante A — schnell (httpx, falls Akamai uns wieder lässt)
.venv/bin/python scraper/crawl.py --concurrency 2

# Variante B — robust (echter Chromium-Tab, hält bei längeren Cooldowns durch)
.venv/bin/python scraper/backfill_playwright.py --min-delay 2 --max-delay 5

# Danach KG neu aufbauen
.venv/bin/python kg/ingest.py --rebuild
```

`ingest.py` ist idempotent. Beim Rebuild wird die SQLite-Datei neu erzeugt
und die FTS5-Tabelle neu befüllt; der MCP-Server greift beim nächsten Call
auf die aktuelle Datei zu.

## Wie's intern funktioniert

**SAP-Help-Endpoints, die ich durch Network-Sniffing identifiziert habe** (Mai 2026,
SPA-Version 5.0.0-2026-05-13):

| Endpoint | Liefert |
|---|---|
| `GET /http.svc/deliverableMetadata?product_url=…&deliverable_url=…&topic_url=…` | numerische `deliverable_id`, `buildNo`, Version, Sprache |
| `GET /http.svc/pagecontent?deliverable_id=…&buildNo=…&file_path=…[&deliverableInfo=1]` | `currentPage.body` (DITA-HTML), und mit `deliverableInfo=1` zusätzlich der ganze `deliverable.fullToc`-Baum |

Beobachtung: `pagecontent` antwortet HTTP 500 wenn `buildNo` fehlt — den muss
man zwingend aus `deliverableMetadata` ziehen.

**Knowledge-Graph-Schema**:
- Property-Graph in SQLite (`nodes`, `edges`), plus `topic_bodies` für HTML/Text.
- FTS5 (`topics_fts`) für BM25-Volltext, mit Porter-Stemming.
- Node-Typen: `topic`, `module`, `transaction`, `table`, `function`.
- Edge-Typen: `child_of`, `mentions`, `uses_transaction`, `uses_table`,
  `calls_function`, `belongs_to_module`.

**Extraction-Strategie** (`kg/parser.py`):
- Topic-Type aus DITA-CSS-Klassen (`concept` / `task` / `reference`).
- Internal Links: 32-hex `.html`-Anker → `mentions`-Edges. Out-of-Deliverable
  Verweise landen in `unresolved_links` (Debug).
- Tx-Codes: Regex mit TRM-Präfix-Whitelist (`TPM`, `TBB`, `TBC`, `FTR`, `FF`,
  `JBR`, `OT`, `F9`) — vermeidet False Positives wie `INFO`/`END`.
- BAPIs/FMs: Präfix-Whitelist (`BAPI_`, `FTR_`, `TPM_`, `JBR_`, …).
- Tables: konservativ, Whitelist-getrieben (siehe `SAP_TRM_TABLES`).

## Erweiterbar

- **Mehr Tables**: `SAP_TRM_TABLES` in `parser.py` ergänzen, `ingest.py --rebuild`.
- **Embeddings/RAG**: `topic_bodies.body_text` ist FTS-indexiert; für
  semantische Suche kann eine `embeddings`-Spalte + Vektor-Index nachgeschoben werden.
- **Weitere Deliverables**: `crawl.py` nimmt `product_url + deliverable_url`
  als Parameter — z. B. SAP ICM oder BiPRO als zweites Deliverable
  daneben legen, der KG kommt durch denselben Schema-Layer.
- **Term/Glossary-Nodes**: `<dfn>` / Glossar-Topics in `parser.py` zu
  eigenen `term`-Nodes mit `defines`-Edges promoten.
