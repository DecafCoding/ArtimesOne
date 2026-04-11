"""Tests for the Phase 3 M1 dashboard: route, grouping logic, and template filters."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from artimesone.config import Settings
from artimesone.web.filters import first_paragraph, format_duration, relative_date

# ---------------------------------------------------------------------------
# Template filter unit tests
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_minutes_and_seconds(self) -> None:
        assert format_duration(754) == "12:34"

    def test_hours_minutes_seconds(self) -> None:
        assert format_duration(3661) == "1:01:01"

    def test_exact_minute(self) -> None:
        assert format_duration(60) == "1:00"

    def test_under_a_minute(self) -> None:
        assert format_duration(45) == "0:45"

    def test_none_returns_empty(self) -> None:
        assert format_duration(None) == ""

    def test_zero_returns_empty(self) -> None:
        assert format_duration(0) == ""

    def test_negative_returns_empty(self) -> None:
        assert format_duration(-10) == ""

    def test_float_seconds(self) -> None:
        assert format_duration(90.7) == "1:30"


class TestRelativeDate:
    def test_today(self) -> None:
        now_iso = datetime.now(UTC).isoformat()
        assert relative_date(now_iso) == "today"

    def test_yesterday(self) -> None:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert relative_date(yesterday) == "yesterday"

    def test_days_ago(self) -> None:
        three_days = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        assert relative_date(three_days) == "3 days ago"

    def test_older_than_a_week(self) -> None:
        old = datetime(2025, 3, 15, tzinfo=UTC).isoformat()
        result = relative_date(old)
        assert "Mar" in result
        assert "15" in result

    def test_none_returns_empty(self) -> None:
        assert relative_date(None) == ""

    def test_empty_string_returns_empty(self) -> None:
        assert relative_date("") == ""

    def test_invalid_string_returns_original(self) -> None:
        assert relative_date("not-a-date") == "not-a-date"


class TestFirstParagraph:
    def test_single_paragraph(self) -> None:
        assert first_paragraph("Hello world.") == "Hello world."

    def test_multiple_paragraphs(self) -> None:
        text = "First para.\n\nSecond para.\n\nThird."
        assert first_paragraph(text) == "First para."

    def test_leading_whitespace(self) -> None:
        text = "\n\n  First para.  \n\nSecond."
        assert first_paragraph(text) == "First para."

    def test_none_returns_empty(self) -> None:
        assert first_paragraph(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert first_paragraph("") == ""


# ---------------------------------------------------------------------------
# Dashboard route integration tests
# ---------------------------------------------------------------------------


def _seed_source(conn: sqlite3.Connection) -> int:
    """Insert a test source and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', 'UC_test', 'Test Channel', '{}', 1, ?, ?)
        """,
        (now, now),
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
) -> int:
    """Insert a test item and return its ID."""
    now = datetime.now(UTC).isoformat()
    if published_at is None:
        published_at = now
    if url is None:
        url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps({
        "duration_seconds": duration_seconds,
        "thumbnail_url": thumbnail_url,
    })
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (source_id, external_id, title, url, published_at, now, metadata, status, now, now),
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


async def test_empty_dashboard(client: httpx.AsyncClient) -> None:
    """Dashboard shows empty state when no items exist."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "No items yet" in r.text
    assert "Manage sources" in r.text


async def test_dashboard_with_items(
    client: httpx.AsyncClient,
    app: Any,
) -> None:
    """Dashboard shows items grouped by topic with expected elements."""
    settings: Settings = app.state.settings
    from artimesone.db import get_connection

    db_conn = get_connection(app.state.db_path)
    try:
        source_id = _seed_source(db_conn)

        # Summarized item with a tag
        item1_id = _seed_item(
            db_conn,
            source_id,
            external_id="vid1",
            title="Test Video About RAG",
            status="summarized",
        )
        summary_rel = _write_summary(
            settings,
            "vid1",
            "RAG evaluation is important.\n\nSecond paragraph here.",
        )
        db_conn.execute(
            "UPDATE items SET summary_path = ? WHERE id = ?",
            (summary_rel, item1_id),
        )
        db_conn.commit()

        tag_id = _seed_tag(db_conn, "rag", "RAG")
        _link_item_tag(db_conn, item1_id, tag_id)

        # Skipped item
        _seed_item(
            db_conn,
            source_id,
            external_id="vid2",
            title="Very Long Video",
            status="skipped_too_long",
            duration_seconds=7200,
        )

        # Item with no tags
        _seed_item(
            db_conn,
            source_id,
            external_id="vid3",
            title="Untagged Video",
            status="discovered",
        )
    finally:
        db_conn.close()

    r = await client.get("/")
    assert r.status_code == 200

    # Summarized item is present
    assert "Test Video About RAG" in r.text
    assert "RAG evaluation is important." in r.text

    # Topic chip is rendered
    assert "/topics/rag" in r.text

    # Skipped item shows badge
    assert "Very Long Video" in r.text
    assert "Too long" in r.text

    # YouTube links are present
    assert "Watch on YouTube" in r.text

    # Untagged item appears (under Uncategorized)
    assert "Untagged Video" in r.text


async def test_dashboard_htmx_loaded(client: httpx.AsyncClient) -> None:
    """Base template includes the HTMX script tag."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "htmx.org" in r.text


async def test_dashboard_nav_links(client: httpx.AsyncClient) -> None:
    """Navigation includes links to Topics and Runs."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "/topics" in r.text
    assert "/runs" in r.text
