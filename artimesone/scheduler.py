"""APScheduler wiring for scheduled collection runs.

Builds an ``AsyncIOScheduler`` and adds one cron job per enabled source. Each
job opens its own short-lived SQLite connection, creates a ``collection_runs``
row, calls ``collector.discover()``, and finalises the run row with aggregate
status.

Phase 1 only calls ``discover()`` — the ``fetch()`` loop is Phase 2.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from artimesone.collectors import COLLECTORS
from artimesone.db import get_connection

if TYPE_CHECKING:
    from artimesone.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_POLL_CRON = "0 */6 * * *"


def build_scheduler(settings: Settings) -> AsyncIOScheduler:  # noqa: ARG001
    """Create an unstarted :class:`AsyncIOScheduler`.

    The caller (``create_app`` lifespan) is responsible for calling
    ``reload_jobs`` and then ``scheduler.start()``.
    """
    return AsyncIOScheduler()


def reload_jobs(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """Sync scheduler jobs with the current set of enabled sources.

    Removes all existing jobs and re-adds one per enabled source row.
    """
    scheduler.remove_all_jobs()

    db_path = settings.data_dir / "artimesone.db"
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT id, type, config FROM sources WHERE enabled = 1").fetchall()
        for row in rows:
            source_id: int = row["id"]
            config_str: str = row["config"]
            try:
                config: dict[str, object] = json.loads(config_str)
            except (json.JSONDecodeError, TypeError):
                config = {}
            poll_cron = str(config.get("poll_cron", DEFAULT_POLL_CRON))
            try:
                trigger = CronTrigger.from_crontab(poll_cron)
            except ValueError:
                logger.warning(
                    "Invalid cron expression %r for source %s, using default",
                    poll_cron,
                    source_id,
                )
                trigger = CronTrigger.from_crontab(DEFAULT_POLL_CRON)
            scheduler.add_job(
                run_source_collection,
                trigger,
                args=(source_id, settings),
                id=f"source-{source_id}",
                replace_existing=True,
            )
        logger.info("Scheduler reloaded: %d source job(s)", len(rows))
    finally:
        conn.close()


async def run_source_collection(source_id: int, settings: Settings) -> None:
    """Execute a single collection run for *source_id*.

    Opens its own SQLite connection (per the plan: scheduler jobs use
    short-lived connections, not the request-scoped one).
    """
    db_path = settings.data_dir / "artimesone.db"
    conn = get_connection(db_path)
    now_iso = datetime.now(UTC).isoformat()

    try:
        source_row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source_row is None or not source_row["enabled"]:
            logger.info("Source %s not found or disabled, skipping", source_id)
            return

        source_type: str = source_row["type"]
        collector = COLLECTORS.get(source_type)
        if collector is None:
            msg = f"Unknown source type: {source_type}"
            _record_run(conn, source_id, now_iso, "error", 0, 0, msg)
            return

        # Open the run.
        cursor = conn.execute(
            """
            INSERT INTO collection_runs
                (source_id, started_at, status, items_discovered, items_processed)
            VALUES (?, ?, 'running', 0, 0)
            """,
            (source_id, now_iso),
        )
        run_id: int = cursor.lastrowid  # type: ignore[assignment]
        conn.commit()

        try:
            result = await collector.discover(
                dict(source_row),  # type: ignore[arg-type]
                conn,
                settings,
            )
        except Exception as exc:
            logger.exception("Collector discover() raised for source %s", source_id)
            _close_run(conn, run_id, "error", 0, 0, str(exc))
            return

        if result.error is not None:
            status = "error"
            error_message: str | None = result.error
        else:
            status = "success"
            error_message = None

        _close_run(
            conn,
            run_id,
            status,
            result.discovered,
            result.discovered + result.filtered_out,
            error_message,
        )
    finally:
        conn.close()


def _record_run(
    conn: sqlite3.Connection,
    source_id: int,
    started_at: str,
    status: str,
    discovered: int,
    processed: int,
    error_message: str | None,
) -> None:
    """Insert a completed collection_runs row in one step (for immediate failures)."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO collection_runs (source_id, started_at, completed_at,
            status, items_discovered, items_processed, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, started_at, now_iso, status, discovered, processed, error_message),
    )
    conn.commit()


def _close_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    discovered: int,
    processed: int,
    error_message: str | None,
) -> None:
    """Finalise an existing collection_runs row."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE collection_runs
        SET completed_at = ?, status = ?, items_discovered = ?,
            items_processed = ?, error_message = ?
        WHERE id = ?
        """,
        (now_iso, status, discovered, processed, error_message, run_id),
    )
    conn.commit()
