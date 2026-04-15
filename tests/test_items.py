"""Integration tests for /items browse, search, and detail routes."""

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
    thumbnail_url: str | None = "https://img.youtube.com/thumb.jpg",
    url: str | None = None,
    transcript_path: str | None = None,
    summary_path: str | None = None,
) -> int:
    """Insert a test item and return its ID."""
    now = datetime.now(UTC).isoformat()
    if published_at is None:
        published_at = now
    if url is None:
        url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps(
        {
            "duration_seconds": duration_seconds,
            "thumbnail_url": thumbnail_url,
        }
    )
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, transcript_path, summary_path,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
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
            transcript_path,
            summary_path,
            now,
            now,
        ),
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


def _write_content_file(settings: Settings, rel_path: str, text: str) -> None:
    """Write a markdown file with YAML front matter."""
    full_path = settings.content_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\ntitle: test\n---\n\n{text}"
    full_path.write_text(content, encoding="utf-8")


def _get_db_conn(app: Any) -> sqlite3.Connection:
    """Open a short-lived connection to the test app's database."""
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


# ---------------------------------------------------------------------------
# /items list tests
# ---------------------------------------------------------------------------


async def test_items_empty(client: httpx.AsyncClient) -> None:
    """GET /items returns 200 with empty-state message when no items exist."""
    r = await client.get("/items")
    assert r.status_code == 200
    assert "No items found" in r.text


async def test_items_list_with_data(client: httpx.AsyncClient, app: Any) -> None:
    """GET /items returns 200 and shows seeded item titles."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="v1", title="Alpha Video")
        _seed_item(conn, source_id, external_id="v2", title="Beta Video")
    finally:
        conn.close()

    r = await client.get("/items")
    assert r.status_code == 200
    assert "Alpha Video" in r.text
    assert "Beta Video" in r.text


async def test_items_nav_link(client: httpx.AsyncClient) -> None:
    """Navigation bar includes a link to /items."""
    r = await client.get("/")
    assert r.status_code == 200
    assert '"/items"' in r.text or "/items" in r.text


async def test_items_list_hides_shorts(client: httpx.AsyncClient, app: Any) -> None:
    """GET /items never surfaces items with status='skipped_short'."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="real", title="Real Video")
        _seed_item(
            conn,
            source_id,
            external_id="sh",
            title="Secret Short",
            status="skipped_short",
            duration_seconds=45,
        )
    finally:
        conn.close()

    r = await client.get("/items")
    assert r.status_code == 200
    assert "Real Video" in r.text
    assert "Secret Short" not in r.text


async def test_source_detail_hides_shorts(client: httpx.AsyncClient, app: Any) -> None:
    """The source detail page excludes items with status='skipped_short'."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="real", title="Real Video")
        _seed_item(
            conn,
            source_id,
            external_id="sh",
            title="Secret Short",
            status="skipped_short",
            duration_seconds=45,
        )
    finally:
        conn.close()

    r = await client.get(f"/sources/{source_id}")
    assert r.status_code == 200
    assert "Real Video" in r.text
    assert "Secret Short" not in r.text


# ---------------------------------------------------------------------------
# /items/search tests
# ---------------------------------------------------------------------------


async def test_search_empty_query(client: httpx.AsyncClient, app: Any) -> None:
    """GET /items/search?q= returns 200 and falls back to recent items."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="v1", title="Fallback Video")
    finally:
        conn.close()

    r = await client.get("/items/search?q=")
    assert r.status_code == 200
    assert "Fallback Video" in r.text


async def test_search_fts_match(client: httpx.AsyncClient, app: Any) -> None:
    """GET /items/search?q=quantum returns FTS-matched items."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Quantum Computing Intro")
        # Update FTS summary column so search matches on summary text too.
        conn.execute(
            "UPDATE items_fts SET summary = ? WHERE rowid = ?",
            ("Quantum entanglement enables faster computation.", item_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.get("/items/search?q=quantum")
    assert r.status_code == 200
    assert "Quantum Computing Intro" in r.text


async def test_search_special_characters(client: httpx.AsyncClient) -> None:
    """GET /items/search with special characters does not crash."""
    r = await client.get('/items/search?q="unbalanced')
    assert r.status_code == 200


async def test_search_no_match(client: httpx.AsyncClient, app: Any) -> None:
    """FTS search with no matches falls back to recent items."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_item(conn, source_id, external_id="v1", title="Regular Video")
    finally:
        conn.close()

    r = await client.get("/items/search?q=xyznonexistent")
    assert r.status_code == 200
    # Falls back to recent items
    assert "Regular Video" in r.text


# ---------------------------------------------------------------------------
# /items/{id} detail tests
# ---------------------------------------------------------------------------


async def test_item_detail_found(client: httpx.AsyncClient, app: Any) -> None:
    """GET /items/{id} returns 200 with item title."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Detail Test Video")
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Detail Test Video" in r.text
    assert "Test Channel" in r.text


async def test_item_detail_404(client: httpx.AsyncClient) -> None:
    """GET /items/999 returns 404."""
    r = await client.get("/items/999")
    assert r.status_code == 404


async def test_item_detail_with_summary(client: httpx.AsyncClient, app: Any) -> None:
    """Item detail shows summary text from the markdown file."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        summary_rel = "summaries/youtube/v1.md"
        _write_content_file(settings, summary_rel, "This is a great summary.")
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="Summarized Video",
            status="summarized",
            summary_path=summary_rel,
        )
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "This is a great summary." in r.text


async def test_item_detail_with_transcript(client: httpx.AsyncClient, app: Any) -> None:
    """Item detail shows collapsible transcript."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        transcript_rel = "transcripts/youtube/v1.md"
        _write_content_file(settings, transcript_rel, "Hello and welcome to the video.")
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="Transcribed Video",
            status="transcribed",
            transcript_path=transcript_rel,
        )
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Show transcript" in r.text
    assert "Hello and welcome to the video." in r.text


async def test_item_detail_skipped(client: httpx.AsyncClient, app: Any) -> None:
    """Skipped items show the 'Too long' badge."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="Long Video",
            status="skipped_too_long",
            duration_seconds=7200,
        )
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Too long" in r.text


async def test_item_detail_discovered(client: httpx.AsyncClient, app: Any) -> None:
    """Discovered items show 'Awaiting transcript' message."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="New Video",
            status="discovered",
        )
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Awaiting transcript" in r.text


async def test_item_detail_with_tags(client: httpx.AsyncClient, app: Any) -> None:
    """Item detail shows topic chips."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Tagged Video")
        tag_id = _seed_tag(conn, "machine-learning", "Machine Learning")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Machine Learning" in r.text
    assert "/topics/machine-learning" in r.text


async def test_item_detail_youtube_link(client: httpx.AsyncClient, app: Any) -> None:
    """Item detail includes a YouTube link."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Any Video")
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "youtube.com/watch" in r.text
    assert "Watch on YouTube" in r.text
