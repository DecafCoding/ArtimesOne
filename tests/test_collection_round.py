"""Tests for run_collection_round: source selection, 24h cooldown, rotation.

These tests stub :func:`run_source_collection` so we can focus on the
round-level behavior (which sources get selected, in what order, and
whether ``last_check_at`` is updated correctly) without touching any
external APIs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations
from artimesone.scheduler import SOURCES_PER_ROUND, run_collection_round


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "artimesone.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _seed_source(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    enabled: bool = True,
    last_check_at: str | None = None,
) -> int:
    """Insert a YouTube source and return its id."""
    now = "2026-01-01T00:00:00+00:00"
    config = json.dumps({"channel_id": external_id})
    cursor = conn.execute(
        """
        INSERT INTO sources
            (type, external_id, name, config, enabled, last_check_at,
             created_at, updated_at)
        VALUES ('youtube_channel', ?, ?, ?, ?, ?, ?, ?)
        """,
        (external_id, external_id, config, 1 if enabled else 0, last_check_at, now, now),
    )
    conn.commit()
    return int(cursor.lastrowid)  # type: ignore[arg-type]


def _hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def test_round_selects_at_most_five_sources(tmp_path: Path) -> None:
    """Even with 7 eligible sources, only 5 are processed in a single round."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    ids = [_seed_source(conn, f"UC{i}") for i in range(7)]
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    assert stub.await_count == SOURCES_PER_ROUND
    called_ids = [call.args[0] for call in stub.await_args_list]
    assert len(set(called_ids)) == SOURCES_PER_ROUND
    for cid in called_ids:
        assert cid in ids


async def test_round_picks_oldest_last_check_first(tmp_path: Path) -> None:
    """Sources are ordered by last_check_at ASC, NULLs first."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    null_id = _seed_source(conn, "UCnull", last_check_at=None)
    old_id = _seed_source(conn, "UCold", last_check_at=_hours_ago(72))
    mid_id = _seed_source(conn, "UCmid", last_check_at=_hours_ago(48))
    newest_id = _seed_source(conn, "UCnewest", last_check_at=_hours_ago(30))
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    called_order = [call.args[0] for call in stub.await_args_list]
    assert called_order == [null_id, old_id, mid_id, newest_id]


async def test_round_skips_sources_checked_within_24h(tmp_path: Path) -> None:
    """Sources with last_check_at < 24h are excluded from the round."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    fresh_id = _seed_source(conn, "UCfresh", last_check_at=_hours_ago(1))
    stale_id = _seed_source(conn, "UCstale", last_check_at=_hours_ago(25))
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    called_ids = [call.args[0] for call in stub.await_args_list]
    assert stale_id in called_ids
    assert fresh_id not in called_ids


async def test_round_skips_disabled_sources(tmp_path: Path) -> None:
    """Disabled sources are never selected regardless of last_check_at."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    enabled_id = _seed_source(conn, "UCon", enabled=True, last_check_at=None)
    _seed_source(conn, "UCoff", enabled=False, last_check_at=None)
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    called_ids = [call.args[0] for call in stub.await_args_list]
    assert called_ids == [enabled_id]


async def test_round_updates_last_check_at_on_success(tmp_path: Path) -> None:
    """last_check_at is bumped to 'now' after a successful source run."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    source_id = _seed_source(conn, "UC1", last_check_at=None)
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    conn = get_connection(tmp_path / "data" / "artimesone.db")
    row = conn.execute("SELECT last_check_at FROM sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    assert row["last_check_at"] is not None
    # Parses as an ISO timestamp from within the last minute.
    parsed = datetime.fromisoformat(row["last_check_at"])
    assert (datetime.now(UTC) - parsed) < timedelta(minutes=1)


async def test_round_updates_last_check_at_on_failure(tmp_path: Path) -> None:
    """last_check_at is bumped even when run_source_collection raises,
    so a permanently broken source does not block rotation."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    source_id = _seed_source(conn, "UC1", last_check_at=None)
    conn.close()

    stub = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    conn = get_connection(tmp_path / "data" / "artimesone.db")
    row = conn.execute("SELECT last_check_at FROM sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    assert row["last_check_at"] is not None


async def test_round_no_eligible_sources_is_noop(tmp_path: Path) -> None:
    """A round with no eligible sources calls nothing and does not error."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)

    _seed_source(conn, "UCrecent", last_check_at=_hours_ago(1))
    conn.close()

    stub = AsyncMock()
    with patch("artimesone.scheduler.run_source_collection", stub):
        await run_collection_round(settings)

    stub.assert_not_awaited()
