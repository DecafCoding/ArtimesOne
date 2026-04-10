-- ArtimesOne v1 schema. See plan §3.1 for the authoritative table definitions.
-- This file is the only migration in v1; future schema changes go in 0002+.
-- Idempotent against schema_migrations bookkeeping handled by the migration runner.
--
-- Conventions:
--   * Every *_at column is TEXT holding an ISO 8601 UTC timestamp.
--   * status / role / source enums are stored as TEXT (stdlib sqlite3 has no enum type).
--   * Foreign keys require PRAGMA foreign_keys=ON per connection — see db.get_connection.

-- ---------------------------------------------------------------------------
-- Raw collected data (collector + pipeline owned)
-- ---------------------------------------------------------------------------

CREATE TABLE sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    config      TEXT    NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (type, external_id)
);

CREATE TABLE items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id     TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    url             TEXT,
    published_at    TEXT,
    fetched_at      TEXT    NOT NULL,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    status          TEXT    NOT NULL,
    transcript_path TEXT,
    summary_path    TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_items_source_status ON items(source_id, status);
CREATE INDEX idx_items_status        ON items(status);
CREATE INDEX idx_items_published_at  ON items(published_at);

CREATE TABLE collection_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at       TEXT    NOT NULL,
    completed_at     TEXT,
    status           TEXT    NOT NULL,
    items_discovered INTEGER NOT NULL DEFAULT 0,
    items_processed  INTEGER NOT NULL DEFAULT 0,
    error_message    TEXT
);

CREATE INDEX idx_collection_runs_source ON collection_runs(source_id, started_at);

-- ---------------------------------------------------------------------------
-- Topic tagging
-- ---------------------------------------------------------------------------

CREATE TABLE tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE item_tags (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    source     TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, tag_id)
);

-- ---------------------------------------------------------------------------
-- Derived content (agent / user authored)
-- ---------------------------------------------------------------------------

CREATE TABLE rollups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL,
    file_path         TEXT    NOT NULL,
    generated_by      TEXT    NOT NULL,
    generating_prompt TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE rollup_tags (
    rollup_id  INTEGER NOT NULL REFERENCES rollups(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (rollup_id, tag_id)
);

CREATE TABLE rollup_items (
    rollup_id  INTEGER NOT NULL REFERENCES rollups(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id)   ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (rollup_id, item_id)
);

-- ---------------------------------------------------------------------------
-- Chat history (UI state)
-- ---------------------------------------------------------------------------

CREATE TABLE chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    tool_calls TEXT,
    created_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Full-text search (plan §3.1)
-- ---------------------------------------------------------------------------
--
-- Standalone FTS5 table (NOT external-content) mirroring items.title plus the
-- prose summary text. Why standalone instead of external-content with
-- content='items'?
--   * The plan declares an FTS column `summary`, but the `items` table has no
--     `summary` column (summary text lives in a md file under content/summaries/).
--     External-content FTS5 requires every declared FTS column to exist on the
--     backing content table, so external-content mode is unworkable here.
--   * Standalone FTS5 stores its own copy of (title, summary). The cost is a
--     small duplication of title text; the benefit is the schema is frozen in
--     0001 across phases — Phase 2's summarization pipeline simply
--     UPDATEs items_fts with the real summary once the md file is written.
-- Plan §3.1's recommendation (external-content) was an open trade-off and the
-- plan explicitly listed a standalone FTS5 table as an acceptable alternative.

CREATE VIRTUAL TABLE items_fts USING fts5(
    title,
    summary,
    tokenize='porter unicode61'
);

CREATE TRIGGER items_fts_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, '');
END;

CREATE TRIGGER items_fts_ad AFTER DELETE ON items BEGIN
    DELETE FROM items_fts WHERE rowid = old.id;
END;

CREATE TRIGGER items_fts_au AFTER UPDATE OF title ON items BEGIN
    UPDATE items_fts SET title = new.title WHERE rowid = new.id;
END;
