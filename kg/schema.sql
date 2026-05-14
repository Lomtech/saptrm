-- SAP TRM Knowledge Graph schema (SQLite + FTS5).
--
-- Design: a generic property-graph (`nodes` + `edges`), plus a separate
-- `topic_bodies` table so the FTS5 index stays narrow and the graph stays
-- queryable without dragging HTML around.
--
-- Node `type` values:
--   topic          — one SAP Help page (loio)
--   module         — Treasury sub-module (FX, Debt&Inv, Trade Finance, …)
--   transaction    — SAP tx-code (e.g. TPM01)
--   table          — SAP DB table (e.g. VTBFHA)
--   function       — SAP function module / BAPI (e.g. BAPI_*, FTR_*)
--   term           — glossary term
--   img            — IMG / Customizing path
--
-- Edge `type` values:
--   child_of           — ToC parent / child (src is child, dst is parent)
--   mentions           — topic mentions / links to another topic
--   defines            — topic defines a term
--   uses_table         — topic references a DB table
--   uses_transaction   — topic references a tx-code
--   calls_function     — topic references a function/BAPI
--   belongs_to_module  — topic / tx / table → module
--   see_also           — explicit "related links"

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,
    slug        TEXT    NOT NULL,        -- stable id within type (loio, tx-code, table-name, …)
    title       TEXT    NOT NULL,
    props_json  TEXT    NOT NULL DEFAULT '{}',
    UNIQUE(type, slug)
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_title ON nodes(title);

CREATE TABLE IF NOT EXISTS edges (
    src_id      INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL,
    weight      REAL    NOT NULL DEFAULT 1.0,
    props_json  TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (src_id, dst_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);

-- Topic-specific payload (HTML/text/ToC-path). Keeps `nodes` lean.
CREATE TABLE IF NOT EXISTS topic_bodies (
    node_id     INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    loio        TEXT    NOT NULL UNIQUE,
    file_path   TEXT    NOT NULL,
    page_id     INTEGER,
    topic_type  TEXT,                    -- concept | task | reference | unknown
    depth       INTEGER NOT NULL,
    toc_path_json TEXT NOT NULL,
    github_link TEXT,
    body_html   TEXT NOT NULL,
    body_text   TEXT NOT NULL,
    short_desc  TEXT
);
CREATE INDEX IF NOT EXISTS idx_topic_bodies_loio ON topic_bodies(loio);

-- FTS5 over title + body text + toc path. We rebuild on ingest.
CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(
    title,
    body_text,
    toc_path,
    short_desc,
    loio UNINDEXED,
    node_id UNINDEXED,
    tokenize = 'porter unicode61'
);

-- "Mention" candidates that we couldn't resolve to a topic at ingest time
-- (e.g. dangling href to a non-TRM deliverable). Useful for debugging.
CREATE TABLE IF NOT EXISTS unresolved_links (
    src_topic_loio TEXT,
    href           TEXT,
    anchor_text    TEXT,
    reason         TEXT
);
