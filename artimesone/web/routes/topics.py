"""Topic browsing routes — /topics list and /topics/{slug} detail.

Provides a table of all topics with item and rollup counts, and a detail
page showing rollups (top) and items (below) for a single topic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...app import get_db, get_settings
from ...config import Settings

router = APIRouter(prefix="/topics")


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


def _first_paragraph(text: str | None) -> str | None:
    """Extract the first non-empty paragraph from *text*."""
    if not text:
        return None
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped:
            return stripped
    return text.strip() or None


def _enrich_item_row(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    settings: Settings,
) -> dict[str, Any]:
    """Build a template-ready dict from an items row."""
    metadata = _parse_metadata(row["metadata"])
    summary_text = _read_summary_text(settings.content_dir, row["summary_path"])
    return {
        "id": row["id"],
        "external_id": row["external_id"],
        "title": row["title"],
        "url": row["url"],
        "published_at": row["published_at"],
        "status": row["status"],
        "source_id": row["source_id"],
        "source_name": row["source_name"],
        "duration_seconds": metadata.get("duration_seconds"),
        "thumbnail_url": metadata.get("thumbnail_url"),
        "summary": summary_text,
        "topics": _fetch_item_tags(conn, row["id"]),
    }


# ---------------------------------------------------------------------------
# /topics — list all topics
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_topics(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    """Render the topics table with item/rollup counts."""
    rows = conn.execute(
        """
        SELECT t.slug, t.name,
               COUNT(DISTINCT it.item_id) AS item_count,
               COUNT(DISTINCT rt.rollup_id) AS rollup_count,
               MAX(i.published_at) AS last_touched
        FROM tags t
        LEFT JOIN item_tags it ON it.tag_id = t.id
        LEFT JOIN items i ON i.id = it.item_id
        LEFT JOIN rollup_tags rt ON rt.tag_id = t.id
        GROUP BY t.id
        HAVING item_count > 0
        ORDER BY last_touched DESC
        """
    ).fetchall()

    topics = [
        {
            "slug": r["slug"],
            "name": r["name"],
            "item_count": r["item_count"],
            "rollup_count": r["rollup_count"],
            "last_touched": r["last_touched"],
        }
        for r in rows
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "topics.html",
        {"topics": topics},
    )


# ---------------------------------------------------------------------------
# /topics/{slug} — topic detail
# ---------------------------------------------------------------------------


@router.get("/{slug}", response_class=HTMLResponse)
async def topic_detail(
    request: Request,
    slug: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the topic detail page with rollups and items."""
    tag_row = conn.execute("SELECT id, slug, name FROM tags WHERE slug = ?", (slug,)).fetchone()

    if tag_row is None:
        return HTMLResponse("Not found", status_code=404)

    tag = {"id": tag_row["id"], "slug": tag_row["slug"], "name": tag_row["name"]}

    # Rollups for this topic.
    rollup_rows = conn.execute(
        """
        SELECT r.id, r.title, r.file_path, r.generated_by, r.created_at
        FROM rollup_tags rt
        JOIN rollups r ON r.id = rt.rollup_id
        WHERE rt.tag_id = ?
        ORDER BY r.created_at DESC
        """,
        (tag["id"],),
    ).fetchall()

    rollups = [
        {
            "id": r["id"],
            "title": r["title"],
            "generated_by": r["generated_by"],
            "created_at": r["created_at"],
            "snippet": _first_paragraph(_read_summary_text(settings.content_dir, r["file_path"])),
        }
        for r in rollup_rows
    ]

    # Items for this topic. Shorts are never tagged (they never reach the
    # summarize phase), but we filter here defensively so any stray tagging
    # still hides them from the UI.
    item_rows = conn.execute(
        """
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.created_at,
               s.id AS source_id, s.name AS source_name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id
        JOIN sources s ON s.id = i.source_id
        WHERE it.tag_id = ?
          AND i.status != 'skipped_short'
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        """,
        (tag["id"],),
    ).fetchall()

    items = [_enrich_item_row(r, conn, settings) for r in item_rows]

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "topic_detail.html",
        {"tag": tag, "rollups": rollups, "items": items},
    )
