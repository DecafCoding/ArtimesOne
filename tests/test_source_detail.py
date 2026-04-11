"""Integration tests for /sources/{id} source detail route."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

from artimesone.config import Settings

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_source(conn: sqlite3.Connection, *, external_id: str = "UC_test") -> int:
    """Insert a test source and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', ?, 'Test Channel', '{}', 1, ?, ?)
        """,
        (external_id, now, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str,
    title: str,
    status: str = "summarized",
    published_at: str | None = None,
    duration_seconds: int | None = 600,
    summary_path: str | None = None,
) -> int:
    """Insert a test item and return its ID."""
    now = datetime.now(UTC).isoformat()
    if published_at is None:
        published_at = now
    url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps(
        {
            "duration_seconds": duration_seconds,
            "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        }
    )
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, summary_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            source_id,
            external_id,
            title,
            url,
            published_at,
            now,
            metadata,
            status,
            summary_path,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_run(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    status: str = "success",
    discovered: int = 3,
    processed: int = 3,
    error_message: str | None = None,
) -> int:
    """Insert a collection_runs row and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO collection_runs
            (source_id, started_at, completed_at, status,
             items_discovered, items_processed, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, now, now, status, discovered, processed, error_message),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_tag(conn: sqlite3.Connection, slug: str, name: str) -> int:
    """Insert a tag and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO tags (slug, name, created_at) VALUES (?, ?, ?)",
        (slug, name, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _link_item_tag(conn: sqlite3.Connection, item_id: int, tag_id: int) -> None:
    """Link an item to a tag."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO item_tags (item_id, tag_id, source, created_at) VALUES (?, ?, 'pipeline', ?)",
        (item_id, tag_id, now),
    )
    conn.commit()


def _write_summary(settings: Settings, external_id: str, summary_text: str) -> str:
    """Write a summary markdown file and return its relative path."""
    rel = f"summaries/youtube/{external_id}.md"
    full_path = settings.content_dir / rel
    full_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\ntitle: test\n---\n\n{summary_text}"
    full_path.write_text(content, encoding="utf-8")
    return rel


def _get_db_conn(app: Any) -> sqlite3.Connection:
    """Open a short-lived connection to the test app's database."""
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_source_detail_404(client: httpx.AsyncClient) -> None:
    """GET /sources/999 returns 404 for a missing source."""
    r = await client.get("/sources/999")
    assert r.status_code == 404


async def test_source_detail_shows_metadata(client: httpx.AsyncClient, app: Any) -> None:
    """GET /sources/{id} shows the source name, type, and external ID."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "Test Channel" in r.text
    assert "youtube_channel" in r.text
    assert "UC_test" in r.text
    assert "Enabled" in r.text


async def test_source_detail_shows_items(client: httpx.AsyncClient, app: Any) -> None:
    """Source detail page lists items belonging to this source."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        summary_rel = _write_summary(settings, "v1", "Summary about ML topic.")
        _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="ML Deep Dive",
            summary_path=summary_rel,
        )
        _seed_item(
            conn,
            source_id,
            external_id="v2",
            title="Another Video",
            status="discovered",
        )
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "ML Deep Dive" in r.text
    assert "Another Video" in r.text
    assert "Summary about ML topic." in r.text
    assert "Awaiting transcript" in r.text


async def test_source_detail_shows_run_history(client: httpx.AsyncClient, app: Any) -> None:
    """Source detail page shows collection run history."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_run(conn, source_id, status="success", discovered=5, processed=5)
        _seed_run(
            conn,
            source_id,
            status="error",
            discovered=0,
            processed=0,
            error_message="API timeout",
        )
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "Run History" in r.text
    assert "success" in r.text
    assert "error" in r.text
    assert "API timeout" in r.text


async def test_source_detail_items_filtered_to_source(
    client: httpx.AsyncClient, app: Any
) -> None:
    """Source detail only shows items for the requested source."""
    conn = _get_db_conn(app)
    try:
        source_a = _seed_source(conn, external_id="UC_a")
        source_b = _seed_source(conn, external_id="UC_b")
        _seed_item(conn, source_a, external_id="v1", title="Source A Video")
        _seed_item(conn, source_b, external_id="v2", title="Source B Video")
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_a}")
    assert r.status_code == 200
    assert "Source A Video" in r.text
    assert "Source B Video" not in r.text


async def test_source_detail_empty_items_and_runs(
    client: httpx.AsyncClient, app: Any
) -> None:
    """Source detail with no items or runs shows empty-state messages."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "No items collected yet" in r.text
    assert "No collection runs yet" in r.text


async def test_source_detail_shows_topic_chips(client: httpx.AsyncClient, app: Any) -> None:
    """Items on source detail show topic chips."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Tagged Video")
        tag_id = _seed_tag(conn, "deep-learning", "Deep Learning")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "Deep Learning" in r.text
    assert "/topics/deep-learning" in r.text
