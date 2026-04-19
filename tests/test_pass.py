"""Integration tests for the Phase-7 pass (dismiss) feature.

Exercises: POST /items/{id}/pass and unpass, exclusion from /items default
view, dashboard and topic/source detail views, ``?show=passed`` toggle
revealing dismissed items, and the agent-side search_items regression.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from artimesone.agents.chat import ChatDeps
from artimesone.agents.tools import list_recent_items, search_items
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations

# ---------------------------------------------------------------------------
# Seed helpers (kept local to avoid cross-file fixture coupling)
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
# Route behavior
# ---------------------------------------------------------------------------


async def test_pass_sets_passed_at_and_redirects(client: httpx.AsyncClient, app: Any) -> None:
    """POST /items/{id}/pass sets passed_at and 303-redirects."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="To Pass")
    finally:
        conn.close()

    r = await client.post(f"/items/{item_id}/pass")
    assert r.status_code == 303

    conn = _get_db_conn(app)
    try:
        row = conn.execute("SELECT passed_at FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["passed_at"] is not None
    finally:
        conn.close()


async def test_unpass_clears_passed_at(client: httpx.AsyncClient, app: Any) -> None:
    """POST /items/{id}/unpass clears passed_at."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Passed Video")
        conn.execute(
            "UPDATE items SET passed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), item_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.post(f"/items/{item_id}/unpass")
    assert r.status_code == 303

    conn = _get_db_conn(app)
    try:
        row = conn.execute("SELECT passed_at FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["passed_at"] is None
    finally:
        conn.close()


async def test_pass_redirects_to_referer(client: httpx.AsyncClient, app: Any) -> None:
    """Pass action honors the Referer header for redirect target."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="X")
    finally:
        conn.close()

    r = await client.post(
        f"/items/{item_id}/pass",
        headers={"referer": "http://test/"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "http://test/"


# ---------------------------------------------------------------------------
# Visibility: passed items hidden from feed surfaces
# ---------------------------------------------------------------------------


async def test_passed_item_hidden_from_items_list(client: httpx.AsyncClient, app: Any) -> None:
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="keep", title="Keep Me")
        passed_id = _seed_item(conn, source_id, external_id="bye", title="Dismissed Video")
    finally:
        conn.close()

    # Pass it.
    await client.post(f"/items/{passed_id}/pass")

    r = await client.get("/items")
    assert r.status_code == 200
    assert "Keep Me" in r.text
    assert "Dismissed Video" not in r.text


async def test_show_passed_reveals_passed_items(client: httpx.AsyncClient, app: Any) -> None:
    """?show=passed reveals only the dismissed items and hides active ones."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="keep", title="Still Active")
        passed_id = _seed_item(conn, source_id, external_id="bye", title="Trashed Video")
    finally:
        conn.close()

    await client.post(f"/items/{passed_id}/pass")

    r = await client.get("/items?show=passed")
    assert r.status_code == 200
    assert "Trashed Video" in r.text
    assert "Still Active" not in r.text


async def test_unpass_restores_to_default_feed(client: httpx.AsyncClient, app: Any) -> None:
    """Un-pass returns the item to the default /items list."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="boomerang", title="Restored Video")
    finally:
        conn.close()

    await client.post(f"/items/{item_id}/pass")
    await client.post(f"/items/{item_id}/unpass")

    r = await client.get("/items")
    assert "Restored Video" in r.text


# ---------------------------------------------------------------------------
# Agent search filter
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _ctx(conn: sqlite3.Connection, settings: Settings) -> RunContext[ChatDeps]:
    return RunContext(
        deps=ChatDeps(conn=conn, settings=settings),
        model=TestModel(),
        usage=RunUsage(),
    )


async def test_agent_search_items_excludes_passed(tmp_path: Path) -> None:
    """search_items tool filters out passed items by default."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    _seed_item(conn, source_id, external_id="alive", title="Alive Alpha")
    passed_id = _seed_item(conn, source_id, external_id="dead", title="Passed Alpha")
    conn.execute(
        "UPDATE items SET passed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), passed_id),
    )
    conn.commit()

    results = await search_items(ctx, "Alpha")
    titles = {r.title for r in results}
    assert "Alive Alpha" in titles
    assert "Passed Alpha" not in titles

    conn.close()


async def test_agent_list_recent_items_excludes_passed(tmp_path: Path) -> None:
    """list_recent_items filters out passed items by default."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    _seed_item(conn, source_id, external_id="alive", title="Alive")
    passed_id = _seed_item(conn, source_id, external_id="dead", title="Gone")
    conn.execute(
        "UPDATE items SET passed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), passed_id),
    )
    conn.commit()

    results = await list_recent_items(ctx)
    titles = {r.title for r in results}
    assert "Alive" in titles
    assert "Gone" not in titles

    conn.close()
