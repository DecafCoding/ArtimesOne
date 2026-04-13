"""Integration tests for /runs collection run log route."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

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


def _seed_run(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    status: str = "success",
    discovered: int = 5,
    processed: int = 5,
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


def _get_db_conn(app: Any) -> sqlite3.Connection:
    """Open a short-lived connection to the test app's database."""
    from artimesone.db import get_connection

    return get_connection(app.state.db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_runs_empty(client: httpx.AsyncClient) -> None:
    """GET /runs returns 200 with empty-state message when no runs exist."""
    r = await client.get("/runs")
    assert r.status_code == 200
    assert "No collection runs yet" in r.text
    # Schedule section shows its own empty state when there are no sources.
    assert "No sources configured" in r.text


async def test_runs_schedule_section_never_run_source(client: httpx.AsyncClient, app: Any) -> None:
    """A source with zero runs renders under Schedule with 'never' last-run."""
    conn = _get_db_conn(app)
    try:
        _seed_source(conn)
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    assert "Schedule" in r.text
    assert "Test Channel" in r.text
    assert "never" in r.text


async def test_runs_schedule_section_disabled_source(client: httpx.AsyncClient, app: Any) -> None:
    """Disabled source appears with 'disabled' in Next run and historic status."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_run(conn, source_id, status="success")
        conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    assert "disabled" in r.text
    assert "success" in r.text


async def test_runs_schedule_section_shows_latest_run_only(
    client: httpx.AsyncClient, app: Any
) -> None:
    """When a source has multiple runs, Schedule reflects the most recent status."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        # Older success, then newer error — Schedule should show error.
        older = "2025-01-01T00:00:00+00:00"
        newer = "2026-04-13T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO collection_runs
                (source_id, started_at, completed_at, status,
                 items_discovered, items_processed, error_message)
            VALUES (?, ?, ?, 'success', 5, 5, NULL)
            """,
            (source_id, older, older),
        )
        conn.execute(
            """
            INSERT INTO collection_runs
                (source_id, started_at, completed_at, status,
                 items_discovered, items_processed, error_message)
            VALUES (?, ?, ?, 'error', 0, 0, 'boom')
            """,
            (source_id, newer, newer),
        )
        conn.commit()
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    # The Schedule section uses a badge with class badge-run-error for
    # the latest status; the older success still appears in Recent runs below.
    assert 'badge-run-error">error' in r.text


async def test_runs_with_data(client: httpx.AsyncClient, app: Any) -> None:
    """GET /runs shows collection runs with status and counts."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_run(conn, source_id, status="success", discovered=3, processed=3)
        _seed_run(
            conn,
            source_id,
            status="error",
            discovered=0,
            processed=0,
            error_message="API key invalid",
        )
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    assert "Test Channel" in r.text
    assert "success" in r.text
    assert "error" in r.text
    assert "API key invalid" in r.text


async def test_runs_links_to_source_detail(client: httpx.AsyncClient, app: Any) -> None:
    """Run rows link source names to /sources/{id}."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_run(conn, source_id)
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    assert f"/sources/{source_id}" in r.text


async def test_runs_partial_status(client: httpx.AsyncClient, app: Any) -> None:
    """Partial runs display correctly."""
    conn = _get_db_conn(app)
    try:
        source_id = _seed_source(conn)
        _seed_run(
            conn,
            source_id,
            status="partial",
            discovered=5,
            processed=3,
            error_message="2 item(s) failed",
        )
    finally:
        conn.close()

    r = await client.get("/runs")
    assert r.status_code == 200
    assert "partial" in r.text
    assert "2 item(s) failed" in r.text
