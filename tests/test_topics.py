"""Integration tests for /topics list and /topics/{slug} detail routes."""

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
# /topics list tests
# ---------------------------------------------------------------------------


async def test_topics_empty(client: httpx.AsyncClient) -> None:
    """GET /topics returns 200 with empty-state message when no topics exist."""
    r = await client.get("/topics")
    assert r.status_code == 200
    assert "No topics yet" in r.text


async def test_topics_list_with_data(client: httpx.AsyncClient, app: Any) -> None:
    """GET /topics returns 200 and shows topic names with item counts."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item1_id = _seed_item(conn, source_id, external_id="v1", title="ML Video")
        item2_id = _seed_item(conn, source_id, external_id="v2", title="ML Video 2")
        item3_id = _seed_item(conn, source_id, external_id="v3", title="RAG Video")

        ml_tag_id = _seed_tag(conn, "machine-learning", "Machine Learning")
        rag_tag_id = _seed_tag(conn, "rag", "RAG")

        _link_item_tag(conn, item1_id, ml_tag_id)
        _link_item_tag(conn, item2_id, ml_tag_id)
        _link_item_tag(conn, item3_id, rag_tag_id)
    finally:
        conn.close()

    r = await client.get("/topics")
    assert r.status_code == 200
    assert "Machine Learning" in r.text
    assert "RAG" in r.text
    # ML topic has 2 items
    assert "/topics/machine-learning" in r.text
    assert "/topics/rag" in r.text


async def test_topics_excludes_orphaned_tags(client: httpx.AsyncClient, app: Any) -> None:
    """Tags with zero items do not appear in the topics list."""
    conn = _get_db_conn(app)
    try:
        _seed_tag(conn, "orphan-tag", "Orphan Tag")
    finally:
        conn.close()

    r = await client.get("/topics")
    assert r.status_code == 200
    assert "Orphan Tag" not in r.text


# ---------------------------------------------------------------------------
# /topics/{slug} detail tests
# ---------------------------------------------------------------------------


async def test_topic_detail_found(client: httpx.AsyncClient, app: Any) -> None:
    """GET /topics/{slug} returns 200 with topic name and items."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        summary_rel = _write_summary(settings, "v1", "A summary about quantization.")
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="Quantization Deep Dive",
            summary_path=summary_rel,
        )
        tag_id = _seed_tag(conn, "quantization", "Quantization")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/quantization")
    assert r.status_code == 200
    assert "Quantization" in r.text
    assert "Quantization Deep Dive" in r.text
    assert "A summary about quantization." in r.text


async def test_topic_detail_404(client: httpx.AsyncClient) -> None:
    """GET /topics/nonexistent returns 404."""
    r = await client.get("/topics/nonexistent")
    assert r.status_code == 404


async def test_topic_detail_shows_rollups_placeholder(client: httpx.AsyncClient, app: Any) -> None:
    """Topic detail shows 'No rollups' when none exist (Phase 4 placeholder)."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Some Video")
        tag_id = _seed_tag(conn, "testing", "Testing")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/testing")
    assert r.status_code == 200
    assert "No rollups on this topic yet" in r.text


async def test_topic_detail_shows_item_cards(client: httpx.AsyncClient, app: Any) -> None:
    """Topic detail renders item cards with expected elements."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(
            conn,
            source_id,
            external_id="v1",
            title="Card Test Video",
            status="discovered",
        )
        tag_id = _seed_tag(conn, "cards", "Cards")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/cards")
    assert r.status_code == 200
    assert "Card Test Video" in r.text
    assert "Test Channel" in r.text
    assert "Awaiting transcript" in r.text


async def test_topic_detail_multiple_items(client: httpx.AsyncClient, app: Any) -> None:
    """Topic detail lists multiple items for the same topic."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        tag_id = _seed_tag(conn, "multi", "Multi")

        item1_id = _seed_item(conn, source_id, external_id="v1", title="First Video")
        item2_id = _seed_item(conn, source_id, external_id="v2", title="Second Video")

        _link_item_tag(conn, item1_id, tag_id)
        _link_item_tag(conn, item2_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/multi")
    assert r.status_code == 200
    assert "First Video" in r.text
    assert "Second Video" in r.text


async def test_topic_detail_shows_youtube_links(client: httpx.AsyncClient, app: Any) -> None:
    """Topic detail item cards include YouTube links."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Linked Video")
        tag_id = _seed_tag(conn, "links", "Links")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/links")
    assert r.status_code == 200
    assert "youtube.com/watch" in r.text
    assert "Watch on YouTube" in r.text


async def test_topic_detail_skipped_item(client: httpx.AsyncClient, app: Any) -> None:
    """Skipped items show 'Too long' badge on topic detail."""
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
        tag_id = _seed_tag(conn, "skipped", "Skipped")
        _link_item_tag(conn, item_id, tag_id)
    finally:
        conn.close()

    r = await client.get("/topics/skipped")
    assert r.status_code == 200
    assert "Too long" in r.text


# ---------------------------------------------------------------------------
# Phase-7 visibility: passed + library-filed items hidden from topic detail
# ---------------------------------------------------------------------------


async def test_topic_detail_hides_passed_items(client: httpx.AsyncClient, app: Any) -> None:
    from datetime import UTC, datetime

    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        active_id = _seed_item(conn, source_id, external_id="v1", title="Active Vid")
        passed_id = _seed_item(conn, source_id, external_id="v2", title="Passed Vid")
        tag_id = _seed_tag(conn, "topic-a", "Topic A")
        _link_item_tag(conn, active_id, tag_id)
        _link_item_tag(conn, passed_id, tag_id)
        conn.execute(
            "UPDATE items SET passed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), passed_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.get("/topics/topic-a")
    assert "Active Vid" in r.text
    assert "Passed Vid" not in r.text


async def test_topic_detail_hides_library_filed_items(client: httpx.AsyncClient, app: Any) -> None:
    from artimesone.lists import add_item_to_list, create_list

    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        active_id = _seed_item(conn, source_id, external_id="v1", title="Still Here")
        filed_id = _seed_item(conn, source_id, external_id="v2", title="Filed Away")
        tag_id = _seed_tag(conn, "topic-b", "Topic B")
        _link_item_tag(conn, active_id, tag_id)
        _link_item_tag(conn, filed_id, tag_id)
        lib_id = create_list(conn, "Lib", "library")
        add_item_to_list(conn, filed_id, lib_id)
    finally:
        conn.close()

    r = await client.get("/topics/topic-b")
    assert "Still Here" in r.text
    assert "Filed Away" not in r.text


async def test_topic_detail_shows_project_filed_items(client: httpx.AsyncClient, app: Any) -> None:
    from artimesone.lists import add_item_to_list, create_list

    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Research Vid")
        tag_id = _seed_tag(conn, "topic-c", "Topic C")
        _link_item_tag(conn, item_id, tag_id)
        proj_id = create_list(conn, "Proj", "project")
        add_item_to_list(conn, item_id, proj_id)
    finally:
        conn.close()

    r = await client.get("/topics/topic-c")
    assert "Research Vid" in r.text
