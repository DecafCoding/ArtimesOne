"""APScheduler wiring for scheduled collection runs.

Builds an ``AsyncIOScheduler`` and adds one cron job per enabled source. Each
job opens its own short-lived SQLite connection, creates a ``collection_runs``
row, and runs the three-phase pipeline: discover → fetch → summarize.

Each phase degrades gracefully when its required credentials are absent:
- discover requires ``youtube_api_key``
- fetch requires ``apify_token``
- summarize requires ``openai_api_key``

Items with ``retry_count >= 3`` are skipped. Per-item exceptions are caught so
one failure never crashes the entire run. The ``collection_runs`` row is closed
with aggregate status: ``success`` (all items ok), ``partial`` (mixed),
``error`` (all failed or zero items attempted).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from artimesone.collectors import COLLECTORS, Collector
from artimesone.db import get_connection

if TYPE_CHECKING:
    from artimesone.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_POLL_CRON = "0 */6 * * *"

_JOB_ID_PREFIX = "source-"


def get_next_run_times(
    scheduler: AsyncIOScheduler | None,
) -> dict[int, datetime | None]:
    """Return ``{source_id: next_run_time}`` for all scheduler jobs.

    Reads the live scheduler state — never recomputes from the cron string,
    since APScheduler may have paused, rescheduled, or removed jobs since the
    last :func:`reload_jobs` call. Jobs whose ids don't match the
    ``source-<int>`` convention are ignored. A ``None`` value means the job
    exists but is currently paused.
    """
    if scheduler is None:
        return {}

    result: dict[int, datetime | None] = {}
    for job in scheduler.get_jobs():
        job_id = str(job.id)
        if not job_id.startswith(_JOB_ID_PREFIX):
            continue
        try:
            source_id = int(job_id[len(_JOB_ID_PREFIX) :])
        except ValueError:
            continue
        result[source_id] = job.next_run_time
    return result


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

    Runs three phases in sequence: discover → fetch → summarize. Each phase
    degrades gracefully when credentials are missing. Opens its own SQLite
    connection (per the plan: scheduler jobs use short-lived connections, not
    the request-scoped one).
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

        # --- Phase 1: discover ---
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
            _close_run(
                conn,
                run_id,
                "error",
                result.discovered,
                result.discovered + result.filtered_out,
                result.error,
            )
            return

        items_discovered = result.discovered

        # --- Phase 2: fetch (requires apify_token) ---
        fetch_ok, fetch_fail = await _run_fetch_phase(collector, source_id, conn, settings)

        # --- Phase 3: summarize (requires openai_api_key) ---
        sum_ok, sum_fail = await _run_summarize_phase(source_id, conn, settings)

        # --- Close the run ---
        total_attempted = items_discovered + fetch_ok + fetch_fail + sum_ok + sum_fail
        total_successes = fetch_ok + sum_ok
        total_failures = fetch_fail + sum_fail

        if total_attempted == 0:
            status = "success"
            error_message: str | None = None
        elif total_failures == 0:
            status = "success"
            error_message = None
        elif total_successes > 0:
            status = "partial"
            error_message = f"{total_failures} item(s) failed"
        else:
            status = "error"
            error_message = f"All {total_failures} item(s) failed"

        _close_run(
            conn,
            run_id,
            status,
            items_discovered,
            total_attempted,
            error_message,
        )
    finally:
        conn.close()


async def _run_fetch_phase(
    collector: Collector,
    source_id: int,
    conn: sqlite3.Connection,
    settings: Settings,
) -> tuple[int, int]:
    """Fetch transcripts for discovered / retryable items. Returns (ok, fail)."""
    if settings.apify_token is None:
        logger.info("No APIFY_TOKEN — skipping fetch phase for source %s", source_id)
        return 0, 0

    rows = conn.execute(
        """
        SELECT id FROM items
        WHERE source_id = ?
          AND retry_count < 3
          AND (
              status = 'discovered'
              OR (status = 'error' AND transcript_path IS NULL)
          )
        """,
        (source_id,),
    ).fetchall()

    ok = 0
    fail = 0
    for row in rows:
        item_id: int = row["id"]
        item_row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item_row is None:
            continue
        try:
            fetch_result = await collector.fetch(
                dict(item_row),  # type: ignore[arg-type]
                conn,
                settings,
            )
            if fetch_result.success:
                ok += 1
            else:
                fail += 1
        except Exception:
            logger.exception("fetch() raised for item %s", item_id)
            fail += 1

    return ok, fail


async def _run_summarize_phase(
    source_id: int,
    conn: sqlite3.Connection,
    settings: Settings,
) -> tuple[int, int]:
    """Summarize transcribed / retryable items. Returns (ok, fail)."""
    if settings.openai_api_key is None:
        logger.info("No OPENAI_API_KEY — skipping summarize phase for source %s", source_id)
        return 0, 0

    from artimesone.pipeline.summarize import summarize_item

    rows = conn.execute(
        """
        SELECT id FROM items
        WHERE source_id = ?
          AND retry_count < 3
          AND (
              status = 'transcribed'
              OR (status = 'error' AND transcript_path IS NOT NULL AND summary_path IS NULL)
          )
        """,
        (source_id,),
    ).fetchall()

    ok = 0
    fail = 0
    for row in rows:
        item_id: int = row["id"]
        try:
            success = await summarize_item(item_id, conn, settings)
            if success:
                ok += 1
            else:
                fail += 1
        except Exception:
            logger.exception("summarize_item() raised for item %s", item_id)
            fail += 1

    return ok, fail


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
