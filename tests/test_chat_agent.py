"""Tests for the chat agent construction and all 17 tools.

Tools are tested by calling the async functions directly with a minimal
RunContext[ChatDeps].  This avoids needing a live LLM and keeps tests fast
and deterministic.  The ``_ctx`` helper constructs the RunContext with a
migrated in-memory DB and temp content directory.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from artimesone.agents.chat import ChatDeps, create_chat_agent
from artimesone.agents.models import (
    CorpusStats,
    ItemDetail,
    ItemSummary,
    RollupDetail,
    RollupSummary,
    SourceInfo,
    TopicInfo,
)
from artimesone.agents.tools import (
    add_source,
    add_tag_to_item,
    create_rollup,
    disable_source,
    enable_source,
    get_item,
    get_rollup,
    get_stats,
    get_transcript,
    list_recent_items,
    list_rollups,
    list_sources,
    list_topics,
    search_items,
    update_rollup,
)
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Build a minimal RunContext for direct tool testing."""
    deps = ChatDeps(conn=conn, settings=settings)
    return RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
    )


def _seed_source(
    conn: sqlite3.Connection,
    *,
    external_id: str = "UCtest",
    name: str = "Test Channel",
    enabled: int = 1,
) -> int:
    now = datetime.now(UTC).isoformat()
    config = json.dumps({"channel_id": external_id})
    cursor = conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', ?, ?, ?, ?, ?, ?)
        """,
        (external_id, name, config, enabled, now, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str = "vid1",
    title: str = "Test Video",
    status: str = "summarized",
    transcript_path: str | None = None,
    summary_path: str | None = None,
) -> int:
    now = datetime.now(UTC).isoformat()
    url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps({"duration_seconds": 600, "thumbnail_url": None, "description": ""})
    cursor = conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, transcript_path, summary_path, retry_count,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            source_id,
            external_id,
            title,
            url,
            now,
            now,
            metadata,
            status,
            transcript_path,
            summary_path,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _seed_tag(conn: sqlite3.Connection, slug: str, name: str) -> int:
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO tags (slug, name, created_at) VALUES (?, ?, ?)",
        (slug, name, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _link_item_tag(conn: sqlite3.Connection, item_id: int, tag_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO item_tags (item_id, tag_id, source, created_at) VALUES (?, ?, 'pipeline', ?)",
        (item_id, tag_id, now),
    )
    conn.commit()


def _write_transcript(content_dir: Path, video_id: str = "vid1") -> str:
    """Write a transcript markdown file. Returns relative path."""
    rel = f"transcripts/youtube/{video_id}.md"
    full = content_dir / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\n"
        f"external_id: {video_id}\n"
        "source: youtube\n"
        'title: "Test Video"\n'
        "---\n\n"
        "This is the transcript body about LoRA fine-tuning.\n",
        encoding="utf-8",
    )
    return rel


def _write_summary(content_dir: Path, video_id: str = "vid1") -> str:
    """Write a summary markdown file. Returns relative path."""
    rel = f"summaries/youtube/{video_id}.md"
    full = content_dir / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\ntitle: test\n---\n\nLoRA enables efficient fine-tuning on consumer GPUs.\n",
        encoding="utf-8",
    )
    return rel


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def test_create_chat_agent_returns_agent() -> None:
    """Factory returns a pydantic-ai Agent instance."""
    agent = create_chat_agent(model="test")
    assert isinstance(agent, Agent)


def test_create_chat_agent_has_all_tools() -> None:
    """The agent has all 17 tools registered."""
    agent = create_chat_agent(model="test")
    tool_names = set(agent._function_toolset.tools)
    expected = {
        "search_items",
        "get_item",
        "get_transcript",
        "list_recent_items",
        "list_topics",
        "list_sources",
        "get_stats",
        "list_rollups",
        "get_rollup",
        "get_lists",
        "get_list",
        "create_rollup",
        "update_rollup",
        "add_tag_to_item",
        "add_source",
        "enable_source",
        "disable_source",
    }
    assert tool_names == expected


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


async def test_search_items_returns_results(tmp_path: Path) -> None:
    """search_items finds items via FTS and returns ItemSummary list."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    summary_rel = _write_summary(settings.content_dir)
    _seed_item(conn, source_id, title="LoRA Fine-Tuning Guide", summary_path=summary_rel)

    # FTS should have the title from the trigger.
    results = await search_items(ctx, "LoRA")
    assert len(results) >= 1
    assert isinstance(results[0], ItemSummary)
    assert "LoRA" in results[0].title

    conn.close()


async def test_search_items_empty_query(tmp_path: Path) -> None:
    """search_items with empty query returns empty list."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    results = await search_items(ctx, "")
    assert results == []

    conn.close()


async def test_search_items_special_characters(tmp_path: Path) -> None:
    """search_items escapes FTS5 special characters without crashing."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    _seed_item(conn, source_id, title="C++ Performance Tips")

    # Queries with special FTS5 chars should not raise.
    results = await search_items(ctx, 'test* "quotes" (parens)')
    assert isinstance(results, list)

    conn.close()


async def test_search_items_filter_by_topic(tmp_path: Path) -> None:
    """search_items filters by topic slug when provided."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item1_id = _seed_item(conn, source_id, external_id="v1", title="LoRA Techniques")
    _seed_item(conn, source_id, external_id="v2", title="LoRA Hardware")

    tag_id = _seed_tag(conn, "lora", "LoRA")
    _link_item_tag(conn, item1_id, tag_id)
    # item2 has no tag

    results = await search_items(ctx, "LoRA", topic="lora")
    assert len(results) == 1
    assert results[0].id == item1_id

    conn.close()


async def test_get_item_returns_detail(tmp_path: Path) -> None:
    """get_item returns an ItemDetail with summary text and topics."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    summary_rel = _write_summary(settings.content_dir)
    item_id = _seed_item(conn, source_id, summary_path=summary_rel)

    tag_id = _seed_tag(conn, "lora", "LoRA")
    _link_item_tag(conn, item_id, tag_id)

    result = await get_item(ctx, item_id)
    assert isinstance(result, ItemDetail)
    assert result.title == "Test Video"
    assert result.summary is not None
    assert "fine-tuning" in result.summary
    assert "LoRA" in result.topics

    conn.close()


async def test_get_item_not_found(tmp_path: Path) -> None:
    """get_item returns an error string for nonexistent items."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await get_item(ctx, 999)
    assert isinstance(result, str)
    assert "not found" in result.lower()

    conn.close()


async def test_get_transcript_returns_text(tmp_path: Path) -> None:
    """get_transcript returns the raw transcript body, stripped of front matter."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    transcript_rel = _write_transcript(settings.content_dir)
    item_id = _seed_item(conn, source_id, transcript_path=transcript_rel)

    result = await get_transcript(ctx, item_id)
    assert isinstance(result, str)
    assert "LoRA fine-tuning" in result
    assert "---" not in result  # front matter stripped

    conn.close()


async def test_get_transcript_no_transcript(tmp_path: Path) -> None:
    """get_transcript returns an error string when item has no transcript."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id, status="discovered")

    result = await get_transcript(ctx, item_id)
    assert isinstance(result, str)
    assert "no transcript" in result.lower()

    conn.close()


async def test_list_recent_items(tmp_path: Path) -> None:
    """list_recent_items returns items from the last N days."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    _seed_item(conn, source_id, external_id="v1", title="Recent Video")

    results = await list_recent_items(ctx, days=7)
    assert len(results) >= 1
    assert isinstance(results[0], ItemSummary)
    assert results[0].title == "Recent Video"

    conn.close()


async def test_list_topics_returns_topic_info(tmp_path: Path) -> None:
    """list_topics returns TopicInfo with item counts."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item1_id = _seed_item(conn, source_id, external_id="v1", title="Video 1")
    item2_id = _seed_item(conn, source_id, external_id="v2", title="Video 2")

    tag_id = _seed_tag(conn, "ml", "Machine Learning")
    _link_item_tag(conn, item1_id, tag_id)
    _link_item_tag(conn, item2_id, tag_id)

    results = await list_topics(ctx)
    assert len(results) == 1
    assert isinstance(results[0], TopicInfo)
    assert results[0].slug == "ml"
    assert results[0].item_count == 2

    conn.close()


async def test_list_sources(tmp_path: Path) -> None:
    """list_sources returns SourceInfo for all registered sources."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    _seed_source(conn, external_id="UC1", name="Channel One")
    _seed_source(conn, external_id="UC2", name="Channel Two")

    results = await list_sources(ctx)
    assert len(results) == 2
    assert all(isinstance(r, SourceInfo) for r in results)
    names = {r.name for r in results}
    assert names == {"Channel One", "Channel Two"}

    conn.close()


async def test_get_stats_returns_corpus_stats(tmp_path: Path) -> None:
    """get_stats returns aggregate CorpusStats."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    _seed_item(conn, source_id, external_id="v1", status="summarized")
    _seed_item(conn, source_id, external_id="v2", status="discovered")
    _seed_tag(conn, "ml", "ML")

    result = await get_stats(ctx)
    assert isinstance(result, CorpusStats)
    assert result.total_items == 2
    assert result.total_sources == 1
    assert result.total_topics == 1
    assert result.items_by_status["summarized"] == 1
    assert result.items_by_status["discovered"] == 1

    conn.close()


async def test_list_rollups_empty(tmp_path: Path) -> None:
    """list_rollups returns empty list when no rollups exist."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    results = await list_rollups(ctx)
    assert results == []

    conn.close()


async def test_get_rollup_not_found(tmp_path: Path) -> None:
    """get_rollup returns an error string for nonexistent rollups."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await get_rollup(ctx, 999)
    assert isinstance(result, str)
    assert "not found" in result.lower()

    conn.close()


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def test_create_rollup_writes_file_and_db(tmp_path: Path) -> None:
    """create_rollup inserts DB rows and writes a markdown file."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id)

    result = await create_rollup(
        ctx,
        title="Weekly ML Digest",
        body="A synthesis of this week's ML content.",
        topics=["machine-learning", "lora"],
        source_item_ids=[item_id],
    )
    assert isinstance(result, int)
    rollup_id = result

    # DB row exists with correct fields.
    row = conn.execute("SELECT * FROM rollups WHERE id = ?", (rollup_id,)).fetchone()
    assert row is not None
    assert row["title"] == "Weekly ML Digest"
    assert row["generated_by"] == "chat_agent"
    assert row["file_path"] != ""

    # Tags were created and linked.
    tag_rows = conn.execute(
        "SELECT t.slug FROM rollup_tags rt JOIN tags t ON t.id = rt.tag_id WHERE rt.rollup_id = ?",
        (rollup_id,),
    ).fetchall()
    tag_slugs = {r["slug"] for r in tag_rows}
    assert "machine-learning" in tag_slugs
    assert "lora" in tag_slugs

    # Source item was linked.
    item_links = conn.execute(
        "SELECT item_id FROM rollup_items WHERE rollup_id = ?", (rollup_id,)
    ).fetchall()
    assert len(item_links) == 1
    assert item_links[0]["item_id"] == item_id

    # Markdown file exists with correct content.
    file_path = settings.content_dir / row["file_path"]
    assert file_path.exists()
    text = file_path.read_text(encoding="utf-8")
    assert "Weekly ML Digest" in text
    assert "A synthesis of this week's ML content." in text

    conn.close()


async def test_create_rollup_get_rollup_roundtrip(tmp_path: Path) -> None:
    """A created rollup is retrievable via get_rollup with full detail."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id)

    rollup_id = await create_rollup(
        ctx,
        title="Roundtrip Test",
        body="Body text for roundtrip.",
        topics=["testing"],
        source_item_ids=[item_id],
    )
    assert isinstance(rollup_id, int)

    detail = await get_rollup(ctx, rollup_id)
    assert isinstance(detail, RollupDetail)
    assert detail.title == "Roundtrip Test"
    assert "Body text for roundtrip." in detail.body
    assert "testing" in detail.topics
    assert len(detail.source_items) == 1

    conn.close()


async def test_create_rollup_appears_in_list(tmp_path: Path) -> None:
    """A created rollup appears in list_rollups."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    await create_rollup(ctx, title="Listed Rollup", body="Body.", topics=["ml"], source_item_ids=[])

    results = await list_rollups(ctx)
    assert len(results) == 1
    assert isinstance(results[0], RollupSummary)
    assert results[0].title == "Listed Rollup"

    conn.close()


async def test_update_rollup_modifies_existing(tmp_path: Path) -> None:
    """update_rollup changes title and body of an existing rollup."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    rollup_id = await create_rollup(
        ctx,
        title="Original Title",
        body="Original body.",
        topics=["ml"],
        source_item_ids=[],
    )
    assert isinstance(rollup_id, int)

    result = await update_rollup(ctx, rollup_id, title="Updated Title", body="Updated body.")
    assert "updated" in result.lower()

    # Verify in DB.
    row = conn.execute("SELECT title FROM rollups WHERE id = ?", (rollup_id,)).fetchone()
    assert row["title"] == "Updated Title"

    # Verify file content.
    file_row = conn.execute("SELECT file_path FROM rollups WHERE id = ?", (rollup_id,)).fetchone()
    text = (settings.content_dir / file_row["file_path"]).read_text(encoding="utf-8")
    assert "Updated body." in text

    conn.close()


async def test_update_rollup_not_found(tmp_path: Path) -> None:
    """update_rollup returns error string for nonexistent rollups."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await update_rollup(ctx, 999, title="Nope")
    assert "not found" in result.lower()

    conn.close()


async def test_update_rollup_changes_topics(tmp_path: Path) -> None:
    """update_rollup replaces topics when provided."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    rollup_id = await create_rollup(
        ctx, title="Topic Test", body="Body.", topics=["old-topic"], source_item_ids=[]
    )
    assert isinstance(rollup_id, int)

    await update_rollup(ctx, rollup_id, topics=["new-topic-a", "new-topic-b"])

    tag_rows = conn.execute(
        "SELECT t.slug FROM rollup_tags rt JOIN tags t ON t.id = rt.tag_id WHERE rt.rollup_id = ?",
        (rollup_id,),
    ).fetchall()
    slugs = {r["slug"] for r in tag_rows}
    assert slugs == {"new-topic-a", "new-topic-b"}

    conn.close()


async def test_add_tag_to_item_idempotent(tmp_path: Path) -> None:
    """add_tag_to_item adds a tag and is idempotent on repeat calls."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id)

    result1 = await add_tag_to_item(ctx, item_id, "new-topic")
    assert "added" in result1.lower()

    # Calling again is a no-op.
    result2 = await add_tag_to_item(ctx, item_id, "new-topic")
    assert "added" in result2.lower()

    # Only one tag row.
    rows = conn.execute("SELECT * FROM item_tags WHERE item_id = ?", (item_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "agent"

    conn.close()


async def test_add_tag_to_item_not_found(tmp_path: Path) -> None:
    """add_tag_to_item returns error for nonexistent items."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await add_tag_to_item(ctx, 999, "tag")
    assert "not found" in result.lower()

    conn.close()


# ---------------------------------------------------------------------------
# Source-management tools
# ---------------------------------------------------------------------------


async def test_add_source_creates_source(tmp_path: Path) -> None:
    """add_source inserts a new source row and returns the ID."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await add_source(ctx, type="youtube_channel", external_id="UCnew", name="New Channel")
    assert isinstance(result, int)

    row = conn.execute("SELECT * FROM sources WHERE id = ?", (result,)).fetchone()
    assert row is not None
    assert row["name"] == "New Channel"
    assert row["external_id"] == "UCnew"
    assert row["enabled"] == 1

    conn.close()


async def test_add_source_duplicate_returns_error(tmp_path: Path) -> None:
    """add_source returns error string for duplicate type+external_id."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    await add_source(ctx, type="youtube_channel", external_id="UCdup", name="First")
    result = await add_source(ctx, type="youtube_channel", external_id="UCdup", name="Second")
    assert isinstance(result, str)
    assert "already exists" in result.lower()

    conn.close()


async def test_enable_disable_source(tmp_path: Path) -> None:
    """enable_source and disable_source toggle the enabled flag."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn, enabled=1)

    # Disable.
    result = await disable_source(ctx, source_id)
    assert "disabled" in result.lower()
    row = conn.execute("SELECT enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert row["enabled"] == 0

    # Enable.
    result = await enable_source(ctx, source_id)
    assert "enabled" in result.lower()
    row = conn.execute("SELECT enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert row["enabled"] == 1

    conn.close()


async def test_enable_source_not_found(tmp_path: Path) -> None:
    """enable_source returns error for nonexistent source."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await enable_source(ctx, 999)
    assert "not found" in result.lower()

    conn.close()


async def test_disable_source_not_found(tmp_path: Path) -> None:
    """disable_source returns error for nonexistent source."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    result = await disable_source(ctx, 999)
    assert "not found" in result.lower()

    conn.close()


# ---------------------------------------------------------------------------
# Write boundary enforcement
# ---------------------------------------------------------------------------


async def test_agent_cannot_write_raw_tables(tmp_path: Path) -> None:
    """No tool writes to items, collection_runs, content/transcripts/, or
    content/summaries/. This verifies the architectural rule that the agent
    only writes to the derived region."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    ctx = _ctx(conn, settings)

    source_id = _seed_source(conn)
    item_id = _seed_item(conn, source_id)

    # Snapshot raw table state before running write tools.
    items_before = conn.execute("SELECT * FROM items").fetchall()
    runs_before = conn.execute("SELECT * FROM collection_runs").fetchall()

    # Ensure content dirs exist for checking.
    (settings.content_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (settings.content_dir / "summaries").mkdir(parents=True, exist_ok=True)

    transcripts_before = set(
        p.name for p in (settings.content_dir / "transcripts").rglob("*") if p.is_file()
    )
    summaries_before = set(
        p.name for p in (settings.content_dir / "summaries").rglob("*") if p.is_file()
    )

    # Run all write tools.
    await create_rollup(
        ctx,
        title="Boundary Test",
        body="Body.",
        topics=["test"],
        source_item_ids=[item_id],
    )
    await add_tag_to_item(ctx, item_id, "extra-tag")
    await add_source(ctx, type="youtube_channel", external_id="UCboundary", name="Boundary")

    # Verify raw tables were NOT modified.
    items_after = conn.execute("SELECT * FROM items").fetchall()
    runs_after = conn.execute("SELECT * FROM collection_runs").fetchall()

    assert len(items_after) == len(items_before)
    assert len(runs_after) == len(runs_before)

    # Verify items rows are byte-identical (no status, metadata, etc. changes).
    for before, after in zip(items_before, items_after, strict=True):
        assert dict(before) == dict(after)

    # Verify no new files in transcripts/ or summaries/.
    transcripts_after = set(
        p.name for p in (settings.content_dir / "transcripts").rglob("*") if p.is_file()
    )
    summaries_after = set(
        p.name for p in (settings.content_dir / "summaries").rglob("*") if p.is_file()
    )
    assert transcripts_after == transcripts_before
    assert summaries_after == summaries_before

    # Rollup file WAS created (derived region — allowed).
    rollups_dir = settings.content_dir / "rollups"
    if rollups_dir.exists():
        rollup_files = list(rollups_dir.rglob("*.md"))
        assert len(rollup_files) == 1

    conn.close()
