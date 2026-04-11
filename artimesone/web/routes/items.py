"""Items browse and detail routes — /items, /items/search, /items/{id}.

Provides a paginated list of all items (newest first), an HTMX-powered
FTS5 search endpoint that returns partial HTML, and a detail page for
individual items with summary, collapsible transcript, and metadata.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

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
        "source_name": row["source_name"],
        "duration_seconds": metadata.get("duration_seconds"),
        "thumbnail_url": metadata.get("thumbnail_url"),
        "summary": None,
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
    q: str | None = None,
    topic: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """List all items, newest first, with optional filters."""
    items = _query_items(conn, q=q, topic=topic, status=status)

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "items.html",
        {"items": items, "q": q or "", "topic": topic or "", "status": status or ""},
    )


def _query_items(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch items with optional filters, newest first."""
    clauses: list[str] = []
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

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.created_at,
               s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
        {where}
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [_enrich_item_row(r, conn) for r in rows]


# ---------------------------------------------------------------------------
# HTMX search endpoint
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
async def search_items(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    q: str = "",
) -> HTMLResponse:
    """HTMX partial: return item cards matching an FTS5 query."""
    items: list[dict[str, Any]] = []

    if q.strip():
        items = _fts_search(conn, q.strip())

    if not items:
        # Fall back to recent items when query is empty or FTS matched nothing.
        items = _query_items(conn, limit=20)

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "items_results.html",
        {"items": items, "q": q},
    )


def _fts_search(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    """Run an FTS5 search and return enriched item dicts with snippets."""
    try:
        rows = conn.execute(
            """
            SELECT i.id, i.external_id, i.title, i.url, i.published_at,
                   i.status, i.metadata, i.summary_path, i.created_at,
                   s.name AS source_name,
                   snippet(items_fts, 1, '<mark>', '</mark>', '...', 30) AS search_snippet
            FROM items_fts
            JOIN items i ON i.id = items_fts.rowid
            JOIN sources s ON s.id = i.source_id
            WHERE items_fts MATCH ?
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
        item = _enrich_item_row(r, conn)
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
        SELECT i.*, s.name AS source_name
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
        "source_name": row["source_name"],
        "duration_seconds": metadata.get("duration_seconds"),
        "thumbnail_url": metadata.get("thumbnail_url"),
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
