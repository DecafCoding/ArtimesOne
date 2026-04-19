"""Sources CRUD routes — add, list, enable, disable, delete YouTube channels.

Also provides the source detail page (``/sources/{id}``) with items for this
source and its collection run history.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...app import get_db, get_settings
from ...config import Settings
from ...lists import get_lists_by_kind
from ..filters_sql import build_visibility_filter

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
    type: Annotated[str, Form()],
    external_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    """Insert a new source row. Picked up automatically on the next round."""
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

    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/enable")
async def enable_source(
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> RedirectResponse:
    """Enable a source. Picked up automatically on the next round."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE sources SET enabled = 1, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()
    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/disable")
async def disable_source(
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> RedirectResponse:
    """Disable a source. The round selection filter excludes it automatically."""
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE sources SET enabled = 0, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()
    return RedirectResponse("/sources", status_code=303)


@router.post("/{source_id}/delete")
async def delete_source(
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> RedirectResponse:
    """Delete a source (cascades to items)."""
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    return RedirectResponse("/sources", status_code=303)


# ---------------------------------------------------------------------------
# Source detail
# ---------------------------------------------------------------------------


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    """Safely parse a JSON metadata string."""
    if not raw:
        return {}
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except (json.JSONDecodeError, TypeError):
        return {}


def _read_summary_text(content_dir: Path, summary_path: str | None) -> str | None:
    """Read summary markdown, strip YAML front matter, return the prose."""
    if not summary_path:
        return None
    full_path = content_dir / summary_path
    if not full_path.exists():
        return None
    text = full_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip() or None


def _fetch_item_tags(conn: sqlite3.Connection, item_id: int) -> list[dict[str, str]]:
    """Return tag dicts (slug, name) for a single item."""
    rows = conn.execute(
        """
        SELECT t.slug, t.name
        FROM item_tags it JOIN tags t ON t.id = it.tag_id
        WHERE it.item_id = ?
        ORDER BY t.name
        """,
        (item_id,),
    ).fetchall()
    return [{"slug": r["slug"], "name": r["name"]} for r in rows]


@router.get("/{source_id}", response_class=HTMLResponse)
async def source_detail(
    request: Request,
    source_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the source detail page with items and run history."""
    source_row = conn.execute(
        "SELECT id, type, external_id, name, enabled FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()

    if source_row is None:
        return HTMLResponse("Not found", status_code=404)

    source: dict[str, Any] = dict(source_row)

    # Items for this source (newest first, limit 50). Shorts, passed, and
    # library-filed items are hidden by the shared visibility filter.
    visibility = build_visibility_filter("i")
    item_rows = conn.execute(
        f"""
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.created_at,
               i.passed_at
        FROM items i
        WHERE i.source_id = ?
          AND {visibility}
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT 50
        """,
        (source_id,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in item_rows:
        metadata = _parse_metadata(row["metadata"])
        summary_text = _read_summary_text(settings.content_dir, row["summary_path"])
        items.append(
            {
                "id": row["id"],
                "external_id": row["external_id"],
                "title": row["title"],
                "url": row["url"],
                "published_at": row["published_at"],
                "status": row["status"],
                "duration_seconds": metadata.get("duration_seconds"),
                "thumbnail_url": metadata.get("thumbnail_url"),
                "summary": summary_text,
                "topics": _fetch_item_tags(conn, row["id"]),
                "passed_at": row["passed_at"],
            }
        )

    # Collection runs for this source (most recent first).
    run_rows = conn.execute(
        """
        SELECT id, started_at, completed_at, status,
               items_discovered, items_processed, error_message
        FROM collection_runs
        WHERE source_id = ?
        ORDER BY started_at DESC
        LIMIT 50
        """,
        (source_id,),
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
        }
        for r in run_rows
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "source_detail.html",
        {
            "source": source,
            "items": items,
            "runs": runs,
            "libraries": [dict(r) for r in get_lists_by_kind(conn, "library")],
            "projects": [dict(r) for r in get_lists_by_kind(conn, "project")],
        },
    )
