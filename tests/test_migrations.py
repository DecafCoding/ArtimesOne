"""Tests for artimesone.migrations — schema creation and idempotency."""

from __future__ import annotations

import sqlite3

from artimesone.migrations import apply_migrations


def _in_memory_conn() -> sqlite3.Connection:
    """Get a WAL-less in-memory connection with FKs enabled."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def test_migrations_create_all_tables() -> None:
    """Migration 0001 creates every expected table."""
    conn = _in_memory_conn()
    applied = apply_migrations(conn)
    assert "0001_initial.sql" in applied

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in rows}

    expected = {
        "sources",
        "items",
        "collection_runs",
        "tags",
        "item_tags",
        "rollups",
        "rollup_tags",
        "rollup_items",
        "chat_messages",
        "schema_migrations",
    }
    assert expected.issubset(table_names)
    conn.close()


def test_items_fts_virtual_table_exists() -> None:
    """items_fts is created as a virtual (FTS5) table."""
    conn = _in_memory_conn()
    apply_migrations(conn)

    row = conn.execute("SELECT type, sql FROM sqlite_master WHERE name='items_fts'").fetchone()
    assert row is not None
    assert row["type"] == "table"
    assert "fts5" in row["sql"].lower()
    conn.close()


def test_migrations_are_idempotent() -> None:
    """Running migrations twice applies nothing the second time."""
    conn = _in_memory_conn()
    first = apply_migrations(conn)
    assert len(first) > 0
    second = apply_migrations(conn)
    assert second == []
    conn.close()


def test_items_fts_title_update_trigger() -> None:
    """Updating items.title refreshes items_fts.title via the items_fts_au trigger."""
    conn = _in_memory_conn()
    apply_migrations(conn)

    now = "2026-01-01T00:00:00"
    conn.execute(
        """
        INSERT INTO sources (type, external_id, name, created_at, updated_at)
        VALUES ('youtube_channel', 'UCtest', 'Test', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, fetched_at, metadata, status,
             retry_count, created_at, updated_at)
        VALUES (1, 'vid1', 'Old Title', ?, '{}', 'discovered', 0, ?, ?)
        """,
        (now, now, now),
    )

    # Sanity: the insert trigger populated items_fts with the original title.
    row = conn.execute("SELECT title FROM items_fts WHERE rowid = 1").fetchone()
    assert row["title"] == "Old Title"

    # Update the title and verify the update trigger refreshes items_fts.
    conn.execute("UPDATE items SET title = 'New Title' WHERE id = 1")
    row = conn.execute("SELECT title FROM items_fts WHERE rowid = 1").fetchone()
    assert row["title"] == "New Title"
    conn.close()


def test_foreign_keys_enforced() -> None:
    """Inserting an item with a nonexistent source_id raises IntegrityError."""
    conn = _in_memory_conn()
    apply_migrations(conn)

    try:
        conn.execute(
            """
            INSERT INTO items
                (source_id, external_id, title, fetched_at, metadata, status,
                 retry_count, created_at, updated_at)
            VALUES (9999, 'vid1', 'Orphan', '2026-01-01T00:00:00', '{}',
                    'discovered', 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """
        )
        raise AssertionError("Expected IntegrityError was not raised")  # noqa: TRY301
    except sqlite3.IntegrityError:
        pass  # expected
    finally:
        conn.close()
