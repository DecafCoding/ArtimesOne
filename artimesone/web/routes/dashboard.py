"""Dashboard route — topic-grouped view of recent items.

Implements the ``/`` route per plan §7.2: items from the last 7 days grouped
by topic, with a "today" section called out at the top. Topic groups are
ordered by most-recent item activity.
"""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...app import get_db, get_settings
from ...config import Settings

router = APIRouter()

_VISIBLE_STATUSES = ("summarized", "transcribed", "discovered", "skipped_too_long")


def _query_recent_items(
    conn: sqlite3.Connection,
    settings: Settings,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Fetch items from the last *days* days with source and tag info."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    placeholders = ",".join("?" for _ in _VISIBLE_STATUSES)

    rows = conn.execute(
        f"""
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.created_at,
               s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.created_at >= ?
          AND i.status IN ({placeholders})
        ORDER BY i.published_at DESC, i.created_at DESC
        """,
        (cutoff, *_VISIBLE_STATUSES),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        metadata = _parse_metadata(row["metadata"])
        summary_text = _read_summary_text(settings.content_dir, row["summary_path"])

        # Fetch tags for this item.
        tag_rows = conn.execute(
            """
            SELECT t.slug, t.name
            FROM item_tags it JOIN tags t ON t.id = it.tag_id
            WHERE it.item_id = ?
            ORDER BY t.name
            """,
            (row["id"],),
        ).fetchall()

        items.append(
            {
                "id": row["id"],
                "external_id": row["external_id"],
                "title": row["title"],
                "url": row["url"],
                "published_at": row["published_at"],
                "status": row["status"],
                "source_name": row["source_name"],
                "duration_seconds": metadata.get("duration_seconds"),
                "thumbnail_url": metadata.get("thumbnail_url"),
                "summary": summary_text,
                "topics": [{"slug": t["slug"], "name": t["name"]} for t in tag_rows],
            }
        )

    return items


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
    # Strip YAML front matter.
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip() or None


def _group_by_topic(
    items: list[dict[str, Any]],
) -> OrderedDict[str, dict[str, Any]]:
    """Group items by topic, ordered by most-recent activity.

    Each item appears under every topic it belongs to. Items with no topics
    go under "Uncategorized". Returns an OrderedDict mapping topic slug to
    ``{"name": str, "slug": str, "entries": list}``.
    """
    groups: dict[str, dict[str, Any]] = {}

    for item in items:
        topics = item["topics"]
        if not topics:
            topics = [{"slug": "uncategorized", "name": "Uncategorized"}]
        for topic in topics:
            slug = topic["slug"]
            if slug not in groups:
                groups[slug] = {
                    "name": topic["name"],
                    "slug": slug,
                    "entries": [],
                    "_latest": item.get("published_at") or item.get("created_at") or "",
                }
            groups[slug]["entries"].append(item)
            # Track latest activity per group for ordering.
            candidate = item.get("published_at") or ""
            if candidate > groups[slug]["_latest"]:
                groups[slug]["_latest"] = candidate

    # Sort by most recent activity descending.
    sorted_groups = OrderedDict(
        sorted(groups.items(), key=lambda kv: kv[1]["_latest"], reverse=True)
    )
    # Remove the internal _latest key.
    for group in sorted_groups.values():
        del group["_latest"]

    return sorted_groups


def _split_today(
    groups: OrderedDict[str, dict[str, Any]],
) -> tuple[OrderedDict[str, dict[str, Any]], OrderedDict[str, dict[str, Any]]]:
    """Split topic groups into today's items and the rest."""
    today_str = datetime.now(UTC).date().isoformat()

    today_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    rest_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()

    for slug, group in groups.items():
        today_items = []
        rest_items = []
        for item in group["entries"]:
            pub = item.get("published_at") or ""
            if pub.startswith(today_str):
                today_items.append(item)
            else:
                rest_items.append(item)

        if today_items:
            today_groups[slug] = {
                "name": group["name"],
                "slug": group["slug"],
                "entries": today_items,
            }
        if rest_items:
            rest_groups[slug] = {
                "name": group["name"],
                "slug": group["slug"],
                "entries": rest_items,
            }

    return today_groups, rest_groups


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the topic-grouped dashboard."""
    items = _query_recent_items(conn, settings)
    groups = _group_by_topic(items)
    today_groups, rest_groups = _split_today(groups)

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "dashboard.html",
        {
            "today_groups": today_groups,
            "rest_groups": rest_groups,
            "has_items": len(items) > 0,
        },
    )
