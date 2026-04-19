-- Phase 7: Pass + Lists (Libraries & Projects).
--
-- Adds three things:
--   1. items.passed_at — a nullable ISO timestamp; non-null means the user
--      has dismissed this item. Feed queries hide passed items by default.
--   2. lists — user-curated collections with a kind discriminator. A library
--      is an exclusive consumption bucket (item belongs to at most one);
--      a project is a non-exclusive research collection (item can be in
--      several). The user-facing labels live in the routes; the data layer
--      only knows "kind".
--   3. list_items — the join table, with a composite PK and a BEFORE INSERT
--      trigger that enforces library exclusivity.
--
-- Library exclusivity note
-- ------------------------
-- The plan's first choice was a partial unique index with a subquery
-- predicate (``WHERE list_id IN (SELECT id FROM lists WHERE kind = 'library')``).
-- SQLite rejects subqueries in partial index predicates — the WHERE clause
-- can only reference columns of the indexed table, constants, and
-- deterministic functions (see https://www.sqlite.org/partialindex.html).
-- We therefore use the plan's documented fallback: a BEFORE INSERT trigger
-- on list_items. The trigger aborts when inserting a library membership
-- for an item that is already in a library. The shared lists module
-- (artimesone/lists.py) atomically removes any prior library membership
-- in a transaction before inserting, so normal use never trips the trigger;
-- the trigger is the integrity backstop against programming errors.

ALTER TABLE items ADD COLUMN passed_at TEXT;

CREATE INDEX idx_items_passed_at ON items(passed_at);

CREATE TABLE lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('library', 'project')),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE UNIQUE INDEX ux_lists_name_kind ON lists(name, kind);

CREATE TABLE list_items (
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    added_at TEXT    NOT NULL,
    notes    TEXT,
    PRIMARY KEY (list_id, item_id)
);

CREATE INDEX ix_list_items_item ON list_items(item_id);

CREATE TRIGGER trg_list_items_one_library_per_item
BEFORE INSERT ON list_items
FOR EACH ROW
WHEN (SELECT kind FROM lists WHERE id = NEW.list_id) = 'library'
  AND EXISTS (
      SELECT 1 FROM list_items li
      JOIN lists l ON l.id = li.list_id
      WHERE li.item_id = NEW.item_id
        AND l.kind = 'library'
  )
BEGIN
    SELECT RAISE(ABORT, 'item already belongs to a library');
END;
