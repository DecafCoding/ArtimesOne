"""Collection run log route — /runs.

Shows recent collection runs with source name, timestamps, status,
item counts, and error messages. Ordered by most recent first.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...app import get_db

router = APIRouter(prefix="/runs")


@router.get("", response_class=HTMLResponse)
async def list_runs(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    """Render the collection run log."""
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
        {"runs": runs},
    )
