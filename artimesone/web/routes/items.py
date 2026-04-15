"""Items browse and detail routes — /items, /items/search, /items/{id}.

Provides a paginated list of all items (newest first), an HTMX-powered
FTS5 search endpoint that returns partial HTML, and a detail page for
individual items with summary, collapsible transcript, and metadata.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...app import get_db, get_settings
from ...config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/items")


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


def _enrich_item_row(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    content_dir: Path,
) -> dict[str, Any]:
    """Build a template-ready dict from an items row."""
    metadata = _parse_metadata(row["metadata"])
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
        "view_count": row["view_count"],
        "like_count": row["like_count"],
        "summary": _read_md_text(content_dir, row["summary_path"]),
        "topics": _fetch_item_tags(conn, row["id"]),
    }


def _read_md_text(content_dir: Path, rel_path: str | None) -> str | None:
    """Read a markdown file, strip YAML front matter, return the prose."""
    if not rel_path:
        return None
    full_path = content_dir / rel_path
    if not full_path.exists():
        return None
    text = full_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip() or None


# ---------------------------------------------------------------------------
# Browse route
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_items(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: str | None = None,
    topic: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """List all items, newest first, with optional filters."""
    items = _query_items(conn, settings.content_dir, q=q, topic=topic, status=status)

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "items.html",
        {"items": items, "q": q or "", "topic": topic or "", "status": status or ""},
    )


def _query_items(
    conn: sqlite3.Connection,
    content_dir: Path,
    *,
    q: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch items with optional filters, newest first.

    Always excludes status='skipped_short' — Shorts are tracked in the DB for
    stop-at-known rotation but never surfaced in the UI.
    """
    clauses: list[str] = ["i.status != 'skipped_short'"]
    params: list[object] = []

    if topic:
        clauses.append(
            "i.id IN (SELECT it.item_id FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id WHERE t.slug = ?)"
        )
        params.append(topic)

    if status:
        clauses.append("i.status = ?")
        params.append(status)

    where = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.created_at,
               i.view_count, i.like_count,
               s.id AS source_id, s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
        {where}
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [_enrich_item_row(r, conn, content_dir) for r in rows]


# ---------------------------------------------------------------------------
# HTMX search endpoint
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
async def search_items(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: str = "",
) -> HTMLResponse:
    """HTMX partial: return item cards matching an FTS5 query."""
    items: list[dict[str, Any]] = []

    if q.strip():
        items = _fts_search(conn, q.strip(), settings.content_dir)

    if not items:
        # Fall back to recent items when query is empty or FTS matched nothing.
        items = _query_items(conn, settings.content_dir, limit=20)

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "items_results.html",
        {"items": items, "q": q},
    )


def _fts_search(
    conn: sqlite3.Connection, query: str, content_dir: Path
) -> list[dict[str, Any]]:
    """Run an FTS5 search and return enriched item dicts with snippets."""
    try:
        rows = conn.execute(
            """
            SELECT i.id, i.external_id, i.title, i.url, i.published_at,
                   i.status, i.metadata, i.summary_path, i.created_at,
                   i.view_count, i.like_count,
                   s.id AS source_id, s.name AS source_name,
                   snippet(items_fts, 1, '<mark>', '</mark>', '...', 30) AS search_snippet
            FROM items_fts
            JOIN items i ON i.id = items_fts.rowid
            JOIN sources s ON s.id = i.source_id
            WHERE items_fts MATCH ?
              AND i.status != 'skipped_short'
            ORDER BY bm25(items_fts)
            LIMIT 20
            """,
            (query,),
        ).fetchall()
    except sqlite3.OperationalError:
        logger.debug("FTS5 MATCH failed for query %r, returning empty", query)
        return []

    items: list[dict[str, Any]] = []
    for r in rows:
        item = _enrich_item_row(r, conn, content_dir)
        item["search_snippet"] = r["search_snippet"]
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Item detail route
# ---------------------------------------------------------------------------


@router.get("/{item_id}", response_class=HTMLResponse)
async def item_detail(
    request: Request,
    item_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the item detail page."""
    row = conn.execute(
        """
        SELECT i.*, s.id AS source_id, s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()

    if row is None:
        return HTMLResponse("Not found", status_code=404)

    metadata = _parse_metadata(row["metadata"])
    tags = _fetch_item_tags(conn, item_id)

    summary_text = _read_md_text(settings.content_dir, row["summary_path"])
    transcript_text = _read_md_text(settings.content_dir, row["transcript_path"])

    item: dict[str, Any] = {
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
        "view_count": row["view_count"],
        "like_count": row["like_count"],
        "retry_count": row["retry_count"],
        "last_error": metadata.get("last_error"),
    }

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "item_detail.html",
        {
            "item": item,
            "tags": tags,
            "summary_text": summary_text,
            "transcript_text": transcript_text,
        },
    )


# ---------------------------------------------------------------------------
# Manual retry route
# ---------------------------------------------------------------------------


@router.post("/{item_id}/retry")
async def retry_item(
    item_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> RedirectResponse:
    """Reset a stuck item so the scheduler's retry predicates re-pick it up.

    User-only recovery action per PRD §8 write-boundary matrix. Clears
    ``transcript_path`` and ``summary_path`` so the full pipeline re-runs; the
    stale on-disk md files are left as orphans and can be GC'd later.
    """
    row = conn.execute("SELECT id, metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return RedirectResponse(url="/items", status_code=303)

    metadata = _parse_metadata(row["metadata"])
    metadata.pop("last_error", None)

    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE items
        SET status = 'discovered',
            retry_count = 0,
            transcript_path = NULL,
            summary_path = NULL,
            metadata = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata), now_iso, item_id),
    )
    conn.commit()

    logger.info("Manual retry reset for item %s", item_id)
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)
