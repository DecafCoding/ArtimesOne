"""Tests for the Phase-7 visibility filter helper and library-exclusivity trigger.

The filter is pure-SQL — we build a small fixture of items in each visibility
bucket (unfiled, passed, library-filed, project-filed) and assert the filter
returns exactly the right IDs for each mode.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from artimesone.db import get_connection
from artimesone.migrations import apply_migrations
from artimesone.web.filters_sql import build_visibility_filter


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    c = get_connection(db)
    apply_migrations(c)
    return c


def _seed_source(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', 'UC1', 'Test Channel', ?, 1, ?, ?)
        """,
        (json.dumps({"channel_id": "UC1"}), now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
    *,
    status: str = "summarized",
    passed: bool = False,
) -> int:
    now = datetime.now(UTC).isoformat()
    passed_at = now if passed else None
    cur = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, fetched_at, status, passed_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, external_id, f"Item {external_id}", now, status, passed_at, now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_list(conn: sqlite3.Connection, name: str, kind: str) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO lists (name, kind, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, kind, now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _link(conn: sqlite3.Connection, list_id: int, item_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO list_items (list_id, item_id, added_at) VALUES (?, ?, ?)",
        (list_id, item_id, now),
    )
    conn.commit()


def _query(conn: sqlite3.Connection, *, show_passed: bool = False) -> set[int]:
    where = build_visibility_filter(show_passed=show_passed)
    rows = conn.execute(f"SELECT i.id FROM items i WHERE {where}").fetchall()
    return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Default mode
# ---------------------------------------------------------------------------


def test_default_includes_plain_item(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    plain = _seed_item(conn, source_id, "plain")

    assert _query(conn) == {plain}
    conn.close()


def test_default_excludes_shorts(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    plain = _seed_item(conn, source_id, "plain")
    _seed_item(conn, source_id, "short", status="skipped_short")

    assert _query(conn) == {plain}
    conn.close()


def test_default_excludes_passed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    plain = _seed_item(conn, source_id, "plain")
    _seed_item(conn, source_id, "passed", passed=True)

    assert _query(conn) == {plain}
    conn.close()


def test_default_excludes_library_filed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    plain = _seed_item(conn, source_id, "plain")
    library_item = _seed_item(conn, source_id, "lib")

    lib_id = _seed_list(conn, "Entertainment", "library")
    _link(conn, lib_id, library_item)

    assert _query(conn) == {plain}
    conn.close()


def test_default_includes_project_filed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    plain = _seed_item(conn, source_id, "plain")
    project_item = _seed_item(conn, source_id, "proj")

    proj_id = _seed_list(conn, "AI Skills", "project")
    _link(conn, proj_id, project_item)

    assert _query(conn) == {plain, project_item}
    conn.close()


def test_default_full_matrix(tmp_path: Path) -> None:
    """All four visibility buckets together — only plain + project survive."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)

    plain = _seed_item(conn, source_id, "plain")
    _seed_item(conn, source_id, "short", status="skipped_short")
    _seed_item(conn, source_id, "passed", passed=True)
    library_item = _seed_item(conn, source_id, "lib")
    project_item = _seed_item(conn, source_id, "proj")

    lib_id = _seed_list(conn, "Edu", "library")
    proj_id = _seed_list(conn, "AI", "project")
    _link(conn, lib_id, library_item)
    _link(conn, proj_id, project_item)

    assert _query(conn) == {plain, project_item}
    conn.close()


# ---------------------------------------------------------------------------
# show_passed=True mode
# ---------------------------------------------------------------------------


def test_show_passed_reveals_only_passed(tmp_path: Path) -> None:
    """show_passed=True flips the filter to surface only passed items."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)

    _seed_item(conn, source_id, "plain")
    passed = _seed_item(conn, source_id, "passed", passed=True)

    assert _query(conn, show_passed=True) == {passed}
    conn.close()


def test_show_passed_still_hides_library_filed(tmp_path: Path) -> None:
    """An item that is both passed and library-filed stays hidden in
    show_passed mode — library filing always hides."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)

    passed = _seed_item(conn, source_id, "passed", passed=True)
    passed_and_filed = _seed_item(conn, source_id, "both", passed=True)

    lib_id = _seed_list(conn, "Edu", "library")
    _link(conn, lib_id, passed_and_filed)

    assert _query(conn, show_passed=True) == {passed}
    conn.close()


def test_show_passed_still_hides_shorts(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)

    passed = _seed_item(conn, source_id, "passed", passed=True)
    # A passed short — shouldn't happen in practice but the filter must handle it.
    short_id = _seed_item(conn, source_id, "short", status="skipped_short")
    conn.execute(
        "UPDATE items SET passed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), short_id),
    )
    conn.commit()

    assert _query(conn, show_passed=True) == {passed}
    conn.close()


# ---------------------------------------------------------------------------
# Library exclusivity trigger
# ---------------------------------------------------------------------------


def test_trigger_blocks_second_library_membership(tmp_path: Path) -> None:
    """The BEFORE INSERT trigger rejects adding the same item to two libraries."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, "vid")

    lib_a = _seed_list(conn, "Lib A", "library")
    lib_b = _seed_list(conn, "Lib B", "library")
    _link(conn, lib_a, item_id)

    with pytest.raises(sqlite3.IntegrityError, match="library"):
        _link(conn, lib_b, item_id)

    conn.close()


def test_trigger_allows_multiple_project_memberships(tmp_path: Path) -> None:
    """An item can belong to many projects — only libraries are exclusive."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, "vid")

    proj_a = _seed_list(conn, "Proj A", "project")
    proj_b = _seed_list(conn, "Proj B", "project")
    _link(conn, proj_a, item_id)
    _link(conn, proj_b, item_id)  # second project OK

    rows = conn.execute("SELECT list_id FROM list_items WHERE item_id = ?", (item_id,)).fetchall()
    assert {r["list_id"] for r in rows} == {proj_a, proj_b}
    conn.close()


def test_trigger_allows_library_plus_project(tmp_path: Path) -> None:
    """An item can be in one library AND one or more projects simultaneously."""
    conn = _conn(tmp_path)
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, "vid")

    lib = _seed_list(conn, "Edu", "library")
    proj = _seed_list(conn, "AI", "project")
    _link(conn, lib, item_id)
    _link(conn, proj, item_id)  # project doesn't conflict with library

    rows = conn.execute("SELECT list_id FROM list_items WHERE item_id = ?", (item_id,)).fetchall()
    assert {r["list_id"] for r in rows} == {lib, proj}
    conn.close()
