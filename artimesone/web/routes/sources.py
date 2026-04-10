"""Sources CRUD routes — add, list, enable, disable, delete YouTube channels."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...app import get_db, get_settings
from ...config import Settings
from ...scheduler import reload_jobs

router = APIRouter(prefix="/sources")


def _render_sources(
    request: Request,
    conn: sqlite3.Connection,
    message: str | None = None,
) -> HTMLResponse:
    """Fetch all sources and render the sources page."""
    rows = conn.execute(
        "SELECT id, type, external_id, name, enabled FROM sources ORDER BY id"
    ).fetchall()
    sources = [dict(r) for r in rows]
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "sources.html",
        {"sources": sources, "message": message},
    )


@router.get("", response_class=HTMLResponse)
async def list_sources(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    """List all registered sources."""
    return _render_sources(request, conn)


@router.post("", response_model=None)
async def add_source(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    type: Annotated[str, Form()],
    external_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    """Insert a new source row and reload the scheduler."""
    now_iso = datetime.now(UTC).isoformat()
    config = json.dumps({"channel_id": external_id})
    try:
        conn.execute(
            """
            INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (type, external_id, name, config, now_iso, now_iso),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return _render_sources(
            request, conn, message=f"A source with external ID '{external_id}' already exists."
        )

    reload_jobs(request.app.state.scheduler, settings)
    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/enable")
async def enable_source(
    request: Request,
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Enable a source and reload the scheduler."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE sources SET enabled = 1, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()
    reload_jobs(request.app.state.scheduler, settings)
    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/disable")
async def disable_source(
    request: Request,
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Disable a source and reload the scheduler."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE sources SET enabled = 0, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()
    reload_jobs(request.app.state.scheduler, settings)
    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/delete")
async def delete_source(
    request: Request,
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Delete a source (cascades to items) and reload the scheduler."""
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    reload_jobs(request.app.state.scheduler, settings)
    return RedirectResponse("/sources", status_code=303)
