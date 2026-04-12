"""Integration tests for /rollups list and /rollups/{id} detail routes."""

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


def _seed_rollup(
    conn: sqlite3.Connection,
    *,
    title: str,
    file_path: str = "",
    generated_by: str = "chat_agent",
) -> int:
    """Insert a rollup and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO rollups (title, file_path, generated_by, generating_prompt,
                             created_at, updated_at)
        VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (title, file_path, generated_by, now, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _link_rollup_tag(conn: sqlite3.Connection, rollup_id: int, tag_id: int) -> None:
    """Link a rollup to a tag."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO rollup_tags (rollup_id, tag_id, created_at) VALUES (?, ?, ?)",
        (rollup_id, tag_id, now),
    )
    conn.commit()


def _link_rollup_item(conn: sqlite3.Connection, rollup_id: int, item_id: int) -> None:
    """Link a rollup to a source item."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO rollup_items (rollup_id, item_id, created_at) VALUES (?, ?, ?)",
        (rollup_id, item_id, now),
    )
    conn.commit()


def _write_rollup_file(settings: Settings, rollup_id: int, slug: str, body: str) -> str:
    """Write a rollup markdown file and return its relative path."""
    rel = f"rollups/{rollup_id}-{slug}.md"
    full_path = settings.content_dir / rel
    full_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\nrollup_id: {rollup_id}\ntitle: test\n---\n\n{body}"
    full_path.write_text(content, encoding="utf-8")
    return rel


def _get_db_conn(app: Any) -> sqlite3.Connection:
    """Open a short-lived connection to the test app's database."""
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


# ---------------------------------------------------------------------------
# /rollups list tests
# ---------------------------------------------------------------------------


async def test_rollups_empty(client: httpx.AsyncClient) -> None:
    """GET /rollups returns 200 with empty-state message when no rollups exist."""
    r = await client.get("/rollups")
    assert r.status_code == 200
    assert "No rollups yet" in r.text


async def test_rollups_list_with_data(client: httpx.AsyncClient, app: Any) -> None:
    """GET /rollups returns 200 and shows rollup titles, topics, and source counts."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item1_id = _seed_item(conn, source_id, external_id="v1", title="ML Video 1")
        item2_id = _seed_item(conn, source_id, external_id="v2", title="ML Video 2")

        tag_id = _seed_tag(conn, "machine-learning", "Machine Learning")

        rollup_id = _seed_rollup(conn, title="ML Weekly Synthesis")
        file_rel = _write_rollup_file(
            settings, rollup_id, "ml-weekly-synthesis", "Summary of ML progress."
        )
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))
        conn.commit()

        _link_rollup_tag(conn, rollup_id, tag_id)
        _link_rollup_item(conn, rollup_id, item1_id)
        _link_rollup_item(conn, rollup_id, item2_id)
    finally:
        conn.close()

    r = await client.get("/rollups")
    assert r.status_code == 200
    assert "ML Weekly Synthesis" in r.text
    assert "Machine Learning" in r.text
    assert "chat_agent" in r.text
    # Source count should be 2
    assert ">2<" in r.text.replace(" ", "")


async def test_rollups_filter_by_topic(client: httpx.AsyncClient, app: Any) -> None:
    """GET /rollups?topic= filters rollups by topic slug."""
    conn = _get_db_conn(app)
    try:
        tag_ml = _seed_tag(conn, "ml", "ML")
        tag_rag = _seed_tag(conn, "rag", "RAG")

        rollup_ml = _seed_rollup(conn, title="ML Rollup")
        rollup_rag = _seed_rollup(conn, title="RAG Rollup")

        _link_rollup_tag(conn, rollup_ml, tag_ml)
        _link_rollup_tag(conn, rollup_rag, tag_rag)
    finally:
        conn.close()

    r = await client.get("/rollups?topic=ml")
    assert r.status_code == 200
    assert "ML Rollup" in r.text
    assert "RAG Rollup" not in r.text

    r = await client.get("/rollups?topic=rag")
    assert r.status_code == 200
    assert "RAG Rollup" in r.text
    assert "ML Rollup" not in r.text


async def test_rollups_nav_link(client: httpx.AsyncClient) -> None:
    """The nav bar contains a Rollups link."""
    r = await client.get("/rollups")
    assert r.status_code == 200
    assert 'href="/rollups"' in r.text


# ---------------------------------------------------------------------------
# /rollups/{id} detail tests
# ---------------------------------------------------------------------------


async def test_rollup_detail_found(client: httpx.AsyncClient, app: Any) -> None:
    """GET /rollups/{id} returns 200 with rollup title, body, and metadata."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        rollup_id = _seed_rollup(conn, title="Deep Dive on RAG")
        file_rel = _write_rollup_file(
            settings,
            rollup_id,
            "deep-dive-on-rag",
            "RAG combines retrieval with generation for better answers.",
        )
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))
        conn.commit()
    finally:
        conn.close()

    r = await client.get(f"/rollups/{rollup_id}")
    assert r.status_code == 200
    assert "Deep Dive on RAG" in r.text
    assert "RAG combines retrieval with generation" in r.text
    assert "chat_agent" in r.text


async def test_rollup_detail_404(client: httpx.AsyncClient) -> None:
    """GET /rollups/999 returns 404."""
    r = await client.get("/rollups/999")
    assert r.status_code == 404


async def test_rollup_detail_shows_source_items(client: httpx.AsyncClient, app: Any) -> None:
    """Rollup detail page shows cited source items as clickable cards."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_item(conn, source_id, external_id="v1", title="Source Video One")

        rollup_id = _seed_rollup(conn, title="Synthesis Rollup")
        file_rel = _write_rollup_file(
            settings, rollup_id, "synthesis-rollup", "A synthesis of recent content."
        )
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))
        conn.commit()

        _link_rollup_item(conn, rollup_id, item_id)
    finally:
        conn.close()

    r = await client.get(f"/rollups/{rollup_id}")
    assert r.status_code == 200
    assert "Source Video One" in r.text
    assert f"/items/{item_id}" in r.text
    assert "Sources" in r.text


async def test_rollup_detail_shows_topic_chips(client: httpx.AsyncClient, app: Any) -> None:
    """Rollup detail page shows topic chips linking to /topics/{slug}."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        tag_id = _seed_tag(conn, "quantization", "Quantization")

        rollup_id = _seed_rollup(conn, title="Quantization Rollup")
        file_rel = _write_rollup_file(
            settings, rollup_id, "quantization-rollup", "About quantization."
        )
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))
        conn.commit()

        _link_rollup_tag(conn, rollup_id, tag_id)
    finally:
        conn.close()

    r = await client.get(f"/rollups/{rollup_id}")
    assert r.status_code == 200
    assert "Quantization" in r.text
    assert "/topics/quantization" in r.text


async def test_rollup_detail_no_source_items(client: httpx.AsyncClient, app: Any) -> None:
    """Rollup with no linked items shows 'No source items' message."""
    settings: Settings = app.state.settings
    conn = _get_db_conn(app)
    try:
        rollup_id = _seed_rollup(conn, title="Empty Sources Rollup")
        file_rel = _write_rollup_file(
            settings, rollup_id, "empty-sources", "A rollup with no citations."
        )
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))
        conn.commit()
    finally:
        conn.close()

    r = await client.get(f"/rollups/{rollup_id}")
    assert r.status_code == 200
    assert "No source items linked" in r.text
