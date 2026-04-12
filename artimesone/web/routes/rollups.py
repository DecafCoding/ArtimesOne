"""Rollup browsing routes — /rollups list and /rollups/{id} detail.

View-only pages for rollup documents created by the chat agent. List view
supports optional topic filtering via query parameter. Detail view renders
the rollup body prose and a "Sources" section of cited items.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from ...app import get_db, get_settings
from ...config import Settings

router = APIRouter(prefix="/rollups")


# ---------------------------------------------------------------------------
# Shared helpers
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


def _read_body_text(content_dir: Path, file_path: str | None) -> str | None:
    """Read rollup markdown, strip YAML front matter, return the prose."""
    if not file_path:
        return None
    full_path = content_dir / file_path
    if not full_path.exists():
        return None
    text = full_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip() or None


def _fetch_rollup_tags(conn: sqlite3.Connection, rollup_id: int) -> list[dict[str, str]]:
    """Return tag dicts (slug, name) for a single rollup."""
    rows = conn.execute(
        """
        SELECT t.slug, t.name
        FROM rollup_tags rt JOIN tags t ON t.id = rt.tag_id
        WHERE rt.rollup_id = ?
        ORDER BY t.name
        """,
        (rollup_id,),
    ).fetchall()
    return [{"slug": r["slug"], "name": r["name"]} for r in rows]


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


def _read_summary_text(content_dir: Path, summary_path: str | None) -> str | None:
    """Read item summary markdown, strip YAML front matter, return the prose."""
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


def _enrich_source_item(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    settings: Settings,
) -> dict[str, Any]:
    """Build a template-ready dict from a rollup_items joined row."""
    metadata = _parse_metadata(row["metadata"])
    summary_text = _read_summary_text(settings.content_dir, row["summary_path"])
    return {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "published_at": row["published_at"],
        "status": row["status"],
        "source_name": row["source_name"],
        "duration_seconds": metadata.get("duration_seconds"),
        "thumbnail_url": metadata.get("thumbnail_url"),
        "summary": summary_text,
        "topics": _fetch_item_tags(conn, row["id"]),
    }


# ---------------------------------------------------------------------------
# /rollups — list all rollups
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_rollups(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    topic: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the rollups list, optionally filtered by topic slug."""
    if topic:
        rows = conn.execute(
            """
            SELECT r.id, r.title, r.file_path, r.generated_by, r.created_at
            FROM rollup_tags rt
            JOIN rollups r ON r.id = rt.rollup_id
            JOIN tags t ON t.id = rt.tag_id
            WHERE t.slug = ?
            ORDER BY r.created_at DESC
            """,
            (topic,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT r.id, r.title, r.file_path, r.generated_by, r.created_at
            FROM rollups r
            ORDER BY r.created_at DESC
            """
        ).fetchall()

    rollups: list[dict[str, Any]] = []
    for r in rows:
        tags = _fetch_rollup_tags(conn, r["id"])

        # Count source items for this rollup.
        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM rollup_items WHERE rollup_id = ?",
            (r["id"],),
        ).fetchone()
        source_count: int = count_row["cnt"] if count_row else 0

        rollups.append(
            {
                "id": r["id"],
                "title": r["title"],
                "generated_by": r["generated_by"],
                "created_at": r["created_at"],
                "topics": tags,
                "source_count": source_count,
            }
        )

    # Fetch all topics for the filter links.
    all_topics = conn.execute(
        """
        SELECT DISTINCT t.slug, t.name
        FROM rollup_tags rt
        JOIN tags t ON t.id = rt.tag_id
        ORDER BY t.name
        """
    ).fetchall()
    topic_filters = [{"slug": t["slug"], "name": t["name"]} for t in all_topics]

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "rollups.html",
        {
            "rollups": rollups,
            "topic_filters": topic_filters,
            "active_topic": topic,
        },
    )


# ---------------------------------------------------------------------------
# /rollups/{rollup_id} — rollup detail
# ---------------------------------------------------------------------------


@router.get("/{rollup_id}", response_class=HTMLResponse)
async def rollup_detail(
    request: Request,
    rollup_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the rollup detail page with body and cited source items."""
    row = conn.execute(
        "SELECT id, title, file_path, generated_by, created_at FROM rollups WHERE id = ?",
        (rollup_id,),
    ).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    body = _read_body_text(settings.content_dir, row["file_path"])
    tags = _fetch_rollup_tags(conn, row["id"])

    # Fetch cited source items.
    item_rows = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.published_at, i.status,
               i.metadata, i.summary_path,
               s.name AS source_name
        FROM rollup_items ri
        JOIN items i ON i.id = ri.item_id
        JOIN sources s ON s.id = i.source_id
        WHERE ri.rollup_id = ?
        ORDER BY i.published_at DESC
        """,
        (rollup_id,),
    ).fetchall()

    source_items = [_enrich_source_item(ir, conn, settings) for ir in item_rows]

    rollup = {
        "id": row["id"],
        "title": row["title"],
        "generated_by": row["generated_by"],
        "created_at": row["created_at"],
    }

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "rollup_detail.html",
        {
            "rollup": rollup,
            "body": body,
            "tags": tags,
            "source_items": source_items,
        },
    )
