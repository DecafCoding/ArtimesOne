"""Tests for the manual retry endpoint — POST /items/{id}/retry.

Covers the PRD Phase 6 deliverable "Manual retry button on item detail view":
the POST resets ``status='discovered'``, ``retry_count=0``,
``transcript_path=NULL``, ``summary_path=NULL``, and clears
``metadata.last_error`` so the scheduler's retry predicates re-pick the item
up on the next run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from artimesone.scheduler import run_source_collection

from .test_scheduler_pipeline import (
    _make_conn,
    _make_settings,
    _mock_apify_success,
    _mock_youtube_api,
    _patch_summarizer,
)
from .test_scheduler_pipeline import _seed_source as _seed_sched_source

# ---------------------------------------------------------------------------
# Seed helpers (route-level tests use the app fixture)
# ---------------------------------------------------------------------------


def _get_db_conn(app: Any) -> sqlite3.Connection:
    """Open a short-lived connection to the test app's database."""
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


def _seed_source(conn: sqlite3.Connection) -> int:
    """Insert a test source and return its ID."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', 'UC_retry_test', 'Retry Test Channel', '{}', 1, ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_errored_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str = "vid_err",
    retry_count: int = 3,
    transcript_path: str | None = "transcripts/youtube/vid_err.md",
    summary_path: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> int:
    """Insert an item in 'error' state with a last_error metadata field."""
    now = datetime.now(UTC).isoformat()
    metadata: dict[str, Any] = {
        "duration_seconds": 600,
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "last_error": "HTTPStatusError: 503 Service Unavailable",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, transcript_path, summary_path,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'error', ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            external_id,
            "Errored Video",
            f"https://www.youtube.com/watch?v={external_id}",
            now,
            now,
            json.dumps(metadata),
            retry_count,
            transcript_path,
            summary_path,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Unit tests — route behavior
# ---------------------------------------------------------------------------


async def test_retry_resets_errored_item(client: httpx.AsyncClient, app: Any) -> None:
    """POST /items/{id}/retry on an errored item resets all retry state."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_errored_item(conn, source_id)
    finally:
        conn.close()

    r = await client.post(f"/items/{item_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/items/{item_id}"

    conn = _get_db_conn(app)
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()

    assert row["status"] == "discovered"
    assert row["retry_count"] == 0
    assert row["transcript_path"] is None
    assert row["summary_path"] is None

    metadata = json.loads(row["metadata"])
    assert "last_error" not in metadata


async def test_retry_unknown_item_redirects_to_list(client: httpx.AsyncClient) -> None:
    """POST /items/99999/retry on a missing id returns 303 to /items, not 500."""
    r = await client.post("/items/99999/retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/items"


async def test_retry_preserves_other_metadata(client: httpx.AsyncClient, app: Any) -> None:
    """Retry clears last_error but preserves duration_seconds and thumbnail_url."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_errored_item(conn, source_id)
    finally:
        conn.close()

    r = await client.post(f"/items/{item_id}/retry", follow_redirects=False)
    assert r.status_code == 303

    conn = _get_db_conn(app)
    try:
        row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()

    metadata = json.loads(row["metadata"])
    assert metadata["duration_seconds"] == 600
    assert metadata["thumbnail_url"] == "https://img.youtube.com/thumb.jpg"
    assert "last_error" not in metadata


async def test_retry_idempotent_on_discovered_item(client: httpx.AsyncClient, app: Any) -> None:
    """POST /items/{id}/retry on a non-errored item is harmless (re-writes same state)."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        now = datetime.now(UTC).isoformat()
        metadata = json.dumps({"duration_seconds": 300})
        cursor = conn.execute(
            """
            INSERT INTO items
                (source_id, external_id, title, url, published_at, fetched_at,
                 metadata, status, retry_count, created_at, updated_at)
            VALUES (?, 'vid_disc', 'Discovered Video', 'https://yt/vid_disc', ?, ?,
                    ?, 'discovered', 0, ?, ?)
            """,
            (source_id, now, now, metadata, now, now),
        )
        conn.commit()
        item_id = cursor.lastrowid
    finally:
        conn.close()

    r = await client.post(f"/items/{item_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/items/{item_id}"

    conn = _get_db_conn(app)
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "discovered"
    assert row["retry_count"] == 0


# ---------------------------------------------------------------------------
# Template rendering — retry button visibility
# ---------------------------------------------------------------------------


async def test_retry_button_renders_on_errored_item_detail(
    client: httpx.AsyncClient, app: Any
) -> None:
    """Errored item detail page renders the retry form + last_error message."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        item_id = _seed_errored_item(conn, source_id)
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Collection failed" in r.text
    assert f'action="/items/{item_id}/retry"' in r.text
    assert "Retry now" in r.text
    assert "HTTPStatusError: 503 Service Unavailable" in r.text
    assert "3 automatic retries" in r.text


async def test_retry_button_hidden_on_healthy_item(client: httpx.AsyncClient, app: Any) -> None:
    """Non-errored items do not render the retry form."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        now = datetime.now(UTC).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO items
                (source_id, external_id, title, url, published_at, fetched_at,
                 metadata, status, retry_count, created_at, updated_at)
            VALUES (?, 'vid_ok', 'Healthy Video', 'https://yt/vid_ok', ?, ?,
                    '{}', 'summarized', 0, ?, ?)
            """,
            (source_id, now, now, now, now),
        )
        conn.commit()
        item_id = cursor.lastrowid
    finally:
        conn.close()

    r = await client.get(f"/items/{item_id}")
    assert r.status_code == 200
    assert "Retry now" not in r.text
    assert "Collection failed" not in r.text


# ---------------------------------------------------------------------------
# Integration test — retry POST unsticks the scheduler
# ---------------------------------------------------------------------------


@respx.mock
async def test_retry_unsticks_item_end_to_end(tmp_path: Path) -> None:
    """Retry POST (simulated via SQL reset) + run_source_collection progresses an
    exhausted-retry item past 'error'. Proves the retry reset matches the
    scheduler's fetch predicate.
    """
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_sched_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    now = "2026-01-01T00:00:00+00:00"
    metadata = json.dumps(
        {"duration_seconds": 600, "last_error": "exhausted retries before manual reset"}
    )
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, 'vid_stuck', 'Stuck Video',
                'https://www.youtube.com/watch?v=vid_stuck', ?, ?,
                ?, 'error', 3, ?, ?)
        """,
        (source["id"], now, now, metadata, now, now),
    )
    conn.commit()
    item_id = cursor.lastrowid

    # Sanity: with retry_count=3, the scheduler skips this item.
    _mock_youtube_api(["vid_stuck"])
    _mock_apify_success()
    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 3

    # Manual retry reset — mirrors exactly what POST /items/{id}/retry does.
    conn.execute(
        """
        UPDATE items
        SET status = 'discovered',
            retry_count = 0,
            transcript_path = NULL,
            summary_path = NULL,
            metadata = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps({"duration_seconds": 600}), now, item_id),
    )
    conn.commit()

    respx.reset()
    _mock_youtube_api(["vid_stuck"])
    _mock_apify_success()

    with _patch_summarizer()[0], _patch_summarizer()[1]:
        await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "transcribed"
    assert row["transcript_path"] is not None

    conn.close()
