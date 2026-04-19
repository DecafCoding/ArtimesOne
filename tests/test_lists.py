"""Integration tests for the Phase-7 lists feature (libraries + projects).

Exercises: library/project CRUD via HTTP, add/remove item membership,
library exclusivity (moving an item between libraries), project
non-exclusivity, visibility effects (libraries hide from /items; projects
do not), and cascade behavior on list delete.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from artimesone.lists import (
    ListError,
    add_item_to_list,
    create_list,
    delete_list,
    get_list_by_id,
    get_lists_by_kind,
    remove_item_from_list,
    rename_list,
)

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_source(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO sources (type, external_id, name, config, enabled, "
        "created_at, updated_at) VALUES ('youtube_channel', 'UC1', 'Ch', "
        "'{}', 1, ?, ?)",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str,
    title: str,
    status: str = "summarized",
) -> int:
    now = datetime.now(UTC).isoformat()
    metadata = json.dumps({"duration_seconds": 600, "thumbnail_url": None})
    cur = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            source_id,
            external_id,
            title,
            f"https://www.youtube.com/watch?v={external_id}",
            now,
            now,
            metadata,
            status,
            now,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _get_db_conn(app: Any) -> sqlite3.Connection:
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


# ---------------------------------------------------------------------------
# Shared module: create / rename / delete
# ---------------------------------------------------------------------------


def test_create_list_library_and_project(conn: sqlite3.Connection) -> None:
    lib_id = create_list(conn, "Entertainment", "library")
    proj_id = create_list(conn, "AI Skills", "project")
    assert lib_id > 0 and proj_id > 0
    assert lib_id != proj_id

    libs = get_lists_by_kind(conn, "library")
    projects = get_lists_by_kind(conn, "project")
    assert [row["name"] for row in libs] == ["Entertainment"]
    assert [row["name"] for row in projects] == ["AI Skills"]


def test_create_list_rejects_duplicate_name_within_kind(conn: sqlite3.Connection) -> None:
    create_list(conn, "Entertainment", "library")
    with pytest.raises(ListError):
        create_list(conn, "Entertainment", "library")
    # But a project with the same name is fine — uniqueness is per-kind.
    create_list(conn, "Entertainment", "project")


def test_rename_list_updates_and_rejects_duplicate(conn: sqlite3.Connection) -> None:
    a = create_list(conn, "Alpha", "library")
    create_list(conn, "Beta", "library")

    rename_list(conn, a, "Gamma")
    row = get_list_by_id(conn, a)
    assert row is not None
    assert row["name"] == "Gamma"

    with pytest.raises(ListError):
        rename_list(conn, a, "Beta")


def test_delete_list_cascades_list_items_but_keeps_items(
    conn: sqlite3.Connection,
) -> None:
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Keep me")
    list_id = create_list(conn, "Temp", "project")
    add_item_to_list(conn, item_id, list_id)

    # Precondition: list_items has the row.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM list_items WHERE list_id = ?", (list_id,)
        ).fetchone()["c"]
        == 1
    )

    delete_list(conn, list_id)

    # list_items row gone; item remains.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM list_items WHERE list_id = ?", (list_id,)
        ).fetchone()["c"]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM items WHERE id = ?", (item_id,)).fetchone()["c"]
        == 1
    )


# ---------------------------------------------------------------------------
# Library exclusivity + project non-exclusivity
# ---------------------------------------------------------------------------


def test_add_item_to_second_library_moves_atomically(conn: sqlite3.Connection) -> None:
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Mover")
    a = create_list(conn, "LibA", "library")
    b = create_list(conn, "LibB", "library")

    add_item_to_list(conn, item_id, a)
    add_item_to_list(conn, item_id, b)

    # Only B should hold the item now; A dropped it.
    rows = conn.execute("SELECT list_id FROM list_items WHERE item_id = ?", (item_id,)).fetchall()
    assert [r["list_id"] for r in rows] == [b]


def test_add_item_to_multiple_projects_succeeds(conn: sqlite3.Connection) -> None:
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Many hats")
    p1 = create_list(conn, "P1", "project")
    p2 = create_list(conn, "P2", "project")
    p3 = create_list(conn, "P3", "project")

    for list_id in (p1, p2, p3):
        add_item_to_list(conn, item_id, list_id)

    rows = conn.execute(
        "SELECT list_id FROM list_items WHERE item_id = ? ORDER BY list_id",
        (item_id,),
    ).fetchall()
    assert [r["list_id"] for r in rows] == sorted([p1, p2, p3])


def test_library_and_project_coexist_on_same_item(conn: sqlite3.Connection) -> None:
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Both")
    lib = create_list(conn, "Lib", "library")
    proj = create_list(conn, "Proj", "project")

    add_item_to_list(conn, item_id, lib)
    add_item_to_list(conn, item_id, proj)

    rows = conn.execute(
        "SELECT list_id FROM list_items WHERE item_id = ? ORDER BY list_id",
        (item_id,),
    ).fetchall()
    assert [r["list_id"] for r in rows] == sorted([lib, proj])


def test_remove_item_from_list(conn: sqlite3.Connection) -> None:
    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Gone")
    list_id = create_list(conn, "Temp", "project")

    add_item_to_list(conn, item_id, list_id)
    remove_item_from_list(conn, item_id, list_id)

    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM list_items WHERE item_id = ?", (item_id,)
        ).fetchone()["c"]
        == 0
    )
    # Idempotent — second remove is a no-op, not an error.
    remove_item_from_list(conn, item_id, list_id)


# ---------------------------------------------------------------------------
# HTTP CRUD round-trip
# ---------------------------------------------------------------------------


async def test_create_and_list_library_via_http(client: httpx.AsyncClient, app: Any) -> None:
    r = await client.post("/libraries", data={"name": "Entertainment"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/libraries/")

    r2 = await client.get("/libraries")
    assert r2.status_code == 200
    assert "Entertainment" in r2.text


async def test_create_and_list_project_via_http(client: httpx.AsyncClient, app: Any) -> None:
    r = await client.post("/projects", data={"name": "AI Skills"})
    assert r.status_code == 303

    r2 = await client.get("/projects")
    assert r2.status_code == 200
    assert "AI Skills" in r2.text


async def test_rename_library_via_http(client: httpx.AsyncClient, app: Any) -> None:
    r = await client.post("/libraries", data={"name": "Alpha"})
    list_id = int(r.headers["location"].rsplit("/", 1)[-1])

    r2 = await client.post(f"/libraries/{list_id}/rename", data={"name": "Beta"})
    assert r2.status_code == 303

    r3 = await client.get(f"/libraries/{list_id}")
    assert r3.status_code == 200
    assert "Beta" in r3.text


async def test_delete_library_via_http(client: httpx.AsyncClient, app: Any) -> None:
    r = await client.post("/libraries", data={"name": "Trash"})
    list_id = int(r.headers["location"].rsplit("/", 1)[-1])

    r2 = await client.post(f"/libraries/{list_id}/delete")
    assert r2.status_code == 303
    assert r2.headers["location"] == "/libraries"

    r3 = await client.get(f"/libraries/{list_id}")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# Visibility effects (via /items)
# ---------------------------------------------------------------------------


async def test_library_filed_item_hidden_from_items_list(
    client: httpx.AsyncClient, app: Any
) -> None:
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        keep_id = _seed_item(conn, source_id, external_id="keep", title="Still Here")
        filed_id = _seed_item(conn, source_id, external_id="filed", title="Filed Away")
    finally:
        conn.close()

    r = await client.post("/libraries", data={"name": "Entertainment"})
    library_id = int(r.headers["location"].rsplit("/", 1)[-1])

    await client.post(f"/items/{filed_id}/list", data={"list_id": str(library_id)})

    r2 = await client.get("/items")
    assert "Still Here" in r2.text
    assert "Filed Away" not in r2.text

    # The library detail page should show the filed item.
    r3 = await client.get(f"/libraries/{library_id}")
    assert "Filed Away" in r3.text
    _ = keep_id  # keep_id referenced for clarity


async def test_project_filed_item_stays_in_items_list(client: httpx.AsyncClient, app: Any) -> None:
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="p1", title="Still Visible")
    finally:
        conn.close()

    r = await client.post("/projects", data={"name": "AI Skills"})
    project_id = int(r.headers["location"].rsplit("/", 1)[-1])

    await client.post(f"/items/{item_id}/list", data={"list_id": str(project_id)})

    r2 = await client.get("/items")
    assert "Still Visible" in r2.text

    r3 = await client.get(f"/projects/{project_id}")
    assert "Still Visible" in r3.text


async def test_moving_item_between_libraries_via_http(client: httpx.AsyncClient, app: Any) -> None:
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="m1", title="Mover")
    finally:
        conn.close()

    a = await client.post("/libraries", data={"name": "LibA"})
    a_id = int(a.headers["location"].rsplit("/", 1)[-1])
    b = await client.post("/libraries", data={"name": "LibB"})
    b_id = int(b.headers["location"].rsplit("/", 1)[-1])

    await client.post(f"/items/{item_id}/list", data={"list_id": str(a_id)})
    await client.post(f"/items/{item_id}/list", data={"list_id": str(b_id)})

    # LibA should be empty; LibB should own the item.
    rA = await client.get(f"/libraries/{a_id}")
    rB = await client.get(f"/libraries/{b_id}")
    assert "Mover" not in rA.text
    assert "Mover" in rB.text


async def test_remove_from_list_via_http(client: httpx.AsyncClient, app: Any) -> None:
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="r1", title="Removable")
    finally:
        conn.close()

    r = await client.post("/libraries", data={"name": "Lib"})
    list_id = int(r.headers["location"].rsplit("/", 1)[-1])

    await client.post(f"/items/{item_id}/list", data={"list_id": str(list_id)})
    # Confirm it was filed away from /items.
    r_items = await client.get("/items")
    assert "Removable" not in r_items.text

    await client.post(f"/items/{item_id}/list/{list_id}/remove")
    r_items = await client.get("/items")
    assert "Removable" in r_items.text
