"""Tests for chat agent list tools (``get_lists``, ``get_list``).

Also includes visibility regressions for ``search_items`` / ``list_recent_items``:
library-filed and passed items are excluded; project-filed items remain.

Tools are called directly with a minimal ``RunContext[ChatDeps]`` built from
an in-memory migrated DB — no live LLM required.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from artimesone.agents.chat import ChatDeps
from artimesone.agents.models import ListDetail, ListInfo
from artimesone.agents.tools import (
    get_list,
    get_lists,
    list_recent_items,
    search_items,
)
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.lists import add_item_to_list, create_list
from artimesone.migrations import apply_migrations


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _ctx(conn: sqlite3.Connection, settings: Settings) -> RunContext[ChatDeps]:
    deps = ChatDeps(conn=conn, settings=settings)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def _seed_source(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    config = json.dumps({"channel_id": "UCtest"})
    cur = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', 'UCtest', 'Test Channel', ?, 1, ?, ?)
        """,
        (config, now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str,
    title: str,
    status: str = "summarized",
) -> int:
    now = datetime.now(UTC).isoformat()
    url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps({"duration_seconds": 600, "thumbnail_url": None})
    cur = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (source_id, external_id, title, url, now, now, metadata, status, now, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# get_lists
# ---------------------------------------------------------------------------


async def test_get_lists_returns_all_kinds(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    create_list(conn, "Entertainment", "library")
    create_list(conn, "AI Skills", "project")

    results = await get_lists(ctx)
    assert len(results) == 2
    assert all(isinstance(r, ListInfo) for r in results)
    kinds = {r.kind for r in results}
    assert kinds == {"library", "project"}

    conn.close()


async def test_get_lists_filter_by_kind(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    create_list(conn, "Entertainment", "library")
    create_list(conn, "Education", "library")
    create_list(conn, "AI Skills", "project")

    libraries = await get_lists(ctx, kind="library")
    assert len(libraries) == 2
    assert all(r.kind == "library" for r in libraries)

    projects = await get_lists(ctx, kind="project")
    assert len(projects) == 1
    assert projects[0].name == "AI Skills"


async def test_get_lists_counts_reflect_members(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_a = _seed_item(conn, source_id, external_id="a", title="A")
    item_b = _seed_item(conn, source_id, external_id="b", title="B")

    proj_id = create_list(conn, "Research", "project")
    add_item_to_list(conn, item_a, proj_id)
    add_item_to_list(conn, item_b, proj_id)

    results = await get_lists(ctx, kind="project")
    assert len(results) == 1
    assert results[0].item_count == 2

    conn.close()


async def test_get_lists_rejects_unknown_kind(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    create_list(conn, "Entertainment", "library")
    results = await get_lists(ctx, kind="bogus")
    assert results == []

    conn.close()


# ---------------------------------------------------------------------------
# get_list
# ---------------------------------------------------------------------------


async def test_get_list_returns_member_items(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="v1", title="Research Item")

    list_id = create_list(conn, "AI Skills", "project")
    add_item_to_list(conn, item_id, list_id)

    result = await get_list(ctx, list_id)
    assert isinstance(result, ListDetail)
    assert result.name == "AI Skills"
    assert result.kind == "project"
    assert len(result.items) == 1
    assert result.items[0].title == "Research Item"

    conn.close()


async def test_get_list_not_found(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await get_list(ctx, 999)
    assert isinstance(result, str)
    assert "not found" in result.lower()

    conn.close()


async def test_get_list_empty_has_no_items(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    list_id = create_list(conn, "Empty", "library")
    result = await get_list(ctx, list_id)
    assert isinstance(result, ListDetail)
    assert result.items == []

    conn.close()


# ---------------------------------------------------------------------------
# Visibility regressions for search_items / list_recent_items
# ---------------------------------------------------------------------------


async def test_search_items_excludes_passed(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    keep_id = _seed_item(conn, source_id, external_id="k", title="LoRA Keep")
    pass_id = _seed_item(conn, source_id, external_id="p", title="LoRA Pass")

    conn.execute(
        "UPDATE items SET passed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), pass_id),
    )
    conn.commit()

    results = await search_items(ctx, "LoRA")
    ids = {r.id for r in results}
    assert keep_id in ids
    assert pass_id not in ids

    conn.close()


async def test_search_items_excludes_library_filed(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    keep_id = _seed_item(conn, source_id, external_id="k", title="LoRA Keep")
    filed_id = _seed_item(conn, source_id, external_id="f", title="LoRA Filed")

    lib_id = create_list(conn, "Entertainment", "library")
    add_item_to_list(conn, filed_id, lib_id)

    results = await search_items(ctx, "LoRA")
    ids = {r.id for r in results}
    assert keep_id in ids
    assert filed_id not in ids

    conn.close()


async def test_search_items_includes_project_filed(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, external_id="p", title="LoRA Project")

    proj_id = create_list(conn, "Research", "project")
    add_item_to_list(conn, item_id, proj_id)

    results = await search_items(ctx, "LoRA")
    ids = {r.id for r in results}
    assert item_id in ids

    conn.close()


async def test_list_recent_items_excludes_passed_and_library(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    keep_id = _seed_item(conn, source_id, external_id="k", title="Keep")
    pass_id = _seed_item(conn, source_id, external_id="p", title="Pass")
    filed_id = _seed_item(conn, source_id, external_id="f", title="Filed")
    proj_id_item = _seed_item(conn, source_id, external_id="pj", title="Project")

    conn.execute(
        "UPDATE items SET passed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), pass_id),
    )
    conn.commit()

    lib_id = create_list(conn, "Entertainment", "library")
    add_item_to_list(conn, filed_id, lib_id)
    proj_id = create_list(conn, "Research", "project")
    add_item_to_list(conn, proj_id_item, proj_id)

    results = await list_recent_items(ctx)
    ids = {r.id for r in results}
    assert keep_id in ids
    assert proj_id_item in ids
    assert pass_id not in ids
    assert filed_id not in ids

    conn.close()
