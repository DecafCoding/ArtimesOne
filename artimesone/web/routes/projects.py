"""Projects routes — research collections the user builds toward rollups.

Projects are *non-exclusive*: an item can live in any number of projects.
Project membership does **not** hide items from the main feed — projects are
active work, the user still wants the item visible while they gather more.
Thin CRUD wrapper over ``artimesone.lists``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...app import get_db, get_settings
from ...config import Settings
from ...lists import (
    ListError,
    create_list,
    delete_list,
    get_list_by_id,
    get_lists_by_kind,
    rename_list,
)

router = APIRouter(prefix="/projects")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except (json.JSONDecodeError, TypeError):
        return {}


def _read_summary_text(content_dir: Path, rel_path: str | None) -> str | None:
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


def _fetch_item_tags(conn: sqlite3.Connection, item_id: int) -> list[dict[str, str]]:
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


def _render_projects(
    request: Request,
    conn: sqlite3.Connection,
    message: str | None = None,
) -> HTMLResponse:
    rows = get_lists_by_kind(conn, "project")
    projects = [dict(r) for r in rows]
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "projects.html",
        {"projects": projects, "message": message},
    )


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_projects(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    return _render_projects(request, conn)


@router.post("", response_model=None)
async def create_project(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    name: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    try:
        list_id = create_list(conn, name, "project")
    except ListError as exc:
        return _render_projects(request, conn, message=str(exc))
    return RedirectResponse(f"/projects/{list_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail + edit
# ---------------------------------------------------------------------------


@router.get("/{list_id}", response_class=HTMLResponse)
async def project_detail(
    request: Request,
    list_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    project_row = get_list_by_id(conn, list_id)
    if project_row is None or project_row["kind"] != "project":
        return HTMLResponse("Not found", status_code=404)

    item_rows = conn.execute(
        """
        SELECT i.id, i.external_id, i.title, i.url, i.published_at,
               i.status, i.metadata, i.summary_path, i.passed_at,
               s.id AS source_id, s.name AS source_name,
               li.added_at
        FROM list_items li
        JOIN items i ON i.id = li.item_id
        JOIN sources s ON s.id = i.source_id
        WHERE li.list_id = ?
        ORDER BY li.added_at DESC
        """,
        (list_id,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in item_rows:
        metadata = _parse_metadata(row["metadata"])
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "url": row["url"],
                "published_at": row["published_at"],
                "status": row["status"],
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "duration_seconds": metadata.get("duration_seconds"),
                "thumbnail_url": metadata.get("thumbnail_url"),
                "summary": _read_summary_text(settings.content_dir, row["summary_path"]),
                "topics": _fetch_item_tags(conn, row["id"]),
                "passed_at": row["passed_at"],
                "added_at": row["added_at"],
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "project_detail.html",
        {"project": dict(project_row), "items": items},
    )


@router.post("/{list_id}/rename", response_model=None)
async def rename_project(
    request: Request,
    list_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    name: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    try:
        rename_list(conn, list_id, name)
    except ListError as exc:
        return _render_projects(request, conn, message=str(exc))
    return RedirectResponse(f"/projects/{list_id}", status_code=303)


@router.post("/{list_id}/delete")
async def delete_project(
    list_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> RedirectResponse:
    try:
        delete_list(conn, list_id)
    except ListError:
        pass
    return RedirectResponse("/projects", status_code=303)
