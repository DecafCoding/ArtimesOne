"""Collection run log route — /runs.

Renders two sections:

1. **Schedule** — one row per source with next-run time (from APScheduler),
   last-run status, and time since last run. Covers both enabled and disabled
   sources so the user can diagnose "why isn't this updating?" at a glance.
2. **Recent runs** — the historical ``collection_runs`` log, newest first.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...app import get_db
from ...scheduler import get_next_run_times

router = APIRouter(prefix="/runs")


def _build_schedule(
    conn: sqlite3.Connection,
    request: Request,
) -> list[dict[str, Any]]:
    """Return per-source schedule rows for the Schedule section."""
    next_runs = get_next_run_times(request.app.state.scheduler)

    rows = conn.execute(
        """
        SELECT s.id, s.name, s.enabled,
               (SELECT status FROM collection_runs
                WHERE source_id = s.id
                ORDER BY started_at DESC LIMIT 1) AS last_status,
               (SELECT COALESCE(completed_at, started_at) FROM collection_runs
                WHERE source_id = s.id
                ORDER BY started_at DESC LIMIT 1) AS last_at
        FROM sources s
        ORDER BY s.name
        """
    ).fetchall()

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "enabled": bool(r["enabled"]),
            "next_run": next_runs.get(r["id"]),
            "last_status": r["last_status"],
            "last_at": r["last_at"],
        }
        for r in rows
    ]


@router.get("", response_class=HTMLResponse)
async def list_runs(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    """Render the Schedule section and the collection run log."""
    schedule = _build_schedule(conn, request)

    rows = conn.execute(
        """
        SELECT cr.id, cr.started_at, cr.completed_at, cr.status,
               cr.items_discovered, cr.items_processed, cr.error_message,
               s.id AS source_id, s.name AS source_name
        FROM collection_runs cr
        JOIN sources s ON s.id = cr.source_id
        ORDER BY cr.started_at DESC
        LIMIT 100
        """
    ).fetchall()

    runs: list[dict[str, Any]] = [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "completed_at": r["completed_at"],
            "status": r["status"],
            "items_discovered": r["items_discovered"],
            "items_processed": r["items_processed"],
            "error_message": r["error_message"],
            "source_id": r["source_id"],
            "source_name": r["source_name"],
        }
        for r in rows
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "runs.html",
        {"schedule": schedule, "runs": runs},
    )
