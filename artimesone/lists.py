"""Shared data-layer operations for user-curated lists (libraries + projects).

Lists are user state (alongside ``chat_messages``): neither collectors nor the
chat agent write them. Web routes and the agent's read tools both go through
the functions here so the exclusive-library rule and ``added_at`` bookkeeping
are applied in exactly one place.

Two ``kind`` values are supported:

* ``library`` — exclusive: an item belongs to at most one library at a time.
  ``add_item_to_list`` removes any prior library membership in the same
  transaction before inserting the new row, so moves are atomic.
* ``project`` — non-exclusive: an item may live in any number of projects.

The BEFORE INSERT trigger from migration ``0004`` guards the exclusivity rule
at the database layer; this module's transactional move keeps that trigger
from ever firing during normal user flows.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

ListKind = Literal["library", "project"]


class ListError(Exception):
    """Raised for user-visible list errors (duplicate name, unknown list, etc.)."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_list(conn: sqlite3.Connection, name: str, kind: ListKind) -> int:
    """Create a new list. Raises ``ListError`` on duplicate name within kind."""
    name = name.strip()
    if not name:
        raise ListError("List name cannot be empty.")
    now = _now()
    try:
        cur = conn.execute(
            "INSERT INTO lists (name, kind, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, kind, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ListError(f"A {kind} named {name!r} already exists.") from exc
    return int(cur.lastrowid)  # type: ignore[arg-type]


def rename_list(conn: sqlite3.Connection, list_id: int, new_name: str) -> None:
    """Rename a list. Raises ``ListError`` on duplicate name or missing list."""
    new_name = new_name.strip()
    if not new_name:
        raise ListError("List name cannot be empty.")
    row = conn.execute("SELECT kind FROM lists WHERE id = ?", (list_id,)).fetchone()
    if row is None:
        raise ListError(f"List {list_id} not found.")
    now = _now()
    try:
        conn.execute(
            "UPDATE lists SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, now, list_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ListError(f"A {row['kind']} named {new_name!r} already exists.") from exc


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    """Delete a list. ``list_items`` rows cascade; items survive."""
    row = conn.execute("SELECT id FROM lists WHERE id = ?", (list_id,)).fetchone()
    if row is None:
        raise ListError(f"List {list_id} not found.")
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    conn.commit()


def add_item_to_list(
    conn: sqlite3.Connection,
    item_id: int,
    list_id: int,
    notes: str | None = None,
) -> None:
    """Add an item to a list.

    If the target list is a library, any existing library membership for the
    item is removed in the same transaction so the move is atomic (no window
    where the item is in zero or two libraries). Project adds are plain
    ``INSERT OR IGNORE``.
    """
    list_row = conn.execute("SELECT id, kind FROM lists WHERE id = ?", (list_id,)).fetchone()
    if list_row is None:
        raise ListError(f"List {list_id} not found.")
    item_row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item_row is None:
        raise ListError(f"Item {item_id} not found.")

    kind: str = list_row["kind"]
    now = _now()

    try:
        if kind == "library":
            conn.execute(
                """
                DELETE FROM list_items
                WHERE item_id = ?
                  AND list_id IN (SELECT id FROM lists WHERE kind = 'library')
                  AND list_id != ?
                """,
                (item_id, list_id),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO list_items (list_id, item_id, added_at, notes)
            VALUES (?, ?, ?, ?)
            """,
            (list_id, item_id, now, notes),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ListError(f"Failed to add item to list: {exc}") from exc


def remove_item_from_list(conn: sqlite3.Connection, item_id: int, list_id: int) -> None:
    """Remove an item from a specific list. No-op if not present."""
    conn.execute(
        "DELETE FROM list_items WHERE list_id = ? AND item_id = ?",
        (list_id, item_id),
    )
    conn.commit()


def get_list_by_id(conn: sqlite3.Connection, list_id: int) -> sqlite3.Row | None:
    """Return the list row or ``None``."""
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT id, name, kind, created_at, updated_at FROM lists WHERE id = ?",
        (list_id,),
    ).fetchone()


def get_lists_by_kind(conn: sqlite3.Connection, kind: ListKind | None = None) -> list[sqlite3.Row]:
    """Return lists of the given kind (or all kinds) with member counts,
    alphabetically sorted."""
    if kind is None:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.kind, l.created_at, l.updated_at,
                   COUNT(li.item_id) AS item_count
            FROM lists l
            LEFT JOIN list_items li ON li.list_id = l.id
            GROUP BY l.id
            ORDER BY l.kind, l.name COLLATE NOCASE
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.kind, l.created_at, l.updated_at,
                   COUNT(li.item_id) AS item_count
            FROM lists l
            LEFT JOIN list_items li ON li.list_id = l.id
            WHERE l.kind = ?
            GROUP BY l.id
            ORDER BY l.name COLLATE NOCASE
            """,
            (kind,),
        ).fetchall()
    return list(rows)


def get_lists_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """Return all lists an item is a member of (library + project)."""
    return list(
        conn.execute(
            """
            SELECT l.id, l.name, l.kind, li.added_at
            FROM list_items li
            JOIN lists l ON l.id = li.list_id
            WHERE li.item_id = ?
            ORDER BY l.kind, l.name COLLATE NOCASE
            """,
            (item_id,),
        ).fetchall()
    )
