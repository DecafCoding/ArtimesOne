"""Tests for artimesone.pipeline.summarize — offline via FunctionModel."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from artimesone.agents.summarizer import create_summarizer_agent
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations
from artimesone.pipeline.summarize import (
    _insert_tags,
    _normalize_slug,
    _read_transcript,
    summarize_item,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUMMARY_JSON = json.dumps(
    {
        "summary": "This video covers LoRA fine-tuning on consumer GPUs.",
        "topics": ["lora", "fine-tuning", "large-language-models"],
    }
)


async def _stub_handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=_SUMMARY_JSON)])


def _make_settings(
    tmp_path: Path,
    *,
    openai_api_key: str | None = "fake-key",
) -> Settings:
    kwargs: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "content_dir": tmp_path / "content",
        "youtube_api_key": "fake",
        "_env_file": None,
    }
    if openai_api_key is not None:
        kwargs["OPENAI_API_KEY"] = openai_api_key
    return Settings(**kwargs)  # type: ignore[arg-type]


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _seed_source(conn: sqlite3.Connection) -> dict[str, object]:
    now = "2026-01-01T00:00:00+00:00"
    config = json.dumps({"channel_id": "UCtest"})
    conn.execute(
        """
        INSERT INTO sources (type, external_id, name, config, enabled, created_at, updated_at)
        VALUES ('youtube_channel', 'UCtest', 'Test Channel', ?, 1, ?, ?)
        """,
        (config, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sources WHERE external_id = 'UCtest'").fetchone()
    return dict(row)


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str = "vid1",
    title: str = "Test Video",
    status: str = "transcribed",
    transcript_path: str | None = "transcripts/youtube/vid1.md",
    retry_count: int = 0,
) -> dict[str, object]:
    now = "2026-01-01T00:00:00+00:00"
    url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps({"duration_seconds": 600, "thumbnail_url": None, "description": ""})
    conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, transcript_path, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            retry_count,
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE external_id = ?", (external_id,)).fetchone()
    return dict(row)


def _write_transcript_file(content_dir: Path, video_id: str = "vid1") -> None:
    """Write a sample transcript markdown file on disk."""
    transcript_dir = content_dir / "transcripts" / "youtube"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / f"{video_id}.md").write_text(
        "---\n"
        "item_id: 1\n"
        "external_id: vid1\n"
        "source: youtube\n"
        'title: "Test Video"\n'
        "published_at: 2026-01-01T00:00:00+00:00\n"
        "fetched_at: 2026-01-01T00:00:00+00:00\n"
        "---\n\n"
        "Welcome to the video about LoRA fine-tuning on consumer GPUs.\n"
        "We will cover the key concepts and practical tips.\n",
        encoding="utf-8",
    )


def _make_agent() -> object:
    """Build a summarizer agent backed by FunctionModel for tests."""
    agent = create_summarizer_agent(model="test")
    # Return agent pre-configured to use FunctionModel for predictable output
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_summarize_item_success(tmp_path: Path) -> None:
    """Full pipeline: transcript file → agent → summary md + tags + FTS."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]
    _write_transcript_file(settings.content_dir)

    agent = create_summarizer_agent(model="test")
    with agent.override(model=FunctionModel(_stub_handler)):
        ok = await summarize_item(item["id"], conn, settings, agent=agent)  # type: ignore[arg-type]

    assert ok is True

    # Item status updated.
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "summarized"
    assert row["summary_path"] == "summaries/youtube/vid1.md"

    # Summary file exists with correct content.
    summary_path = settings.content_dir / "summaries" / "youtube" / "vid1.md"
    assert summary_path.exists()
    text = summary_path.read_text(encoding="utf-8")
    assert "---" in text
    assert "external_id: vid1" in text
    assert 'title: "Test Video"' in text
    assert "LoRA fine-tuning" in text

    # Tags inserted.
    tags = conn.execute("SELECT slug FROM tags ORDER BY slug").fetchall()
    slugs = [t["slug"] for t in tags]
    assert "lora" in slugs
    assert "fine-tuning" in slugs
    assert "large-language-models" in slugs

    # item_tags exist with source='pipeline'.
    item_tags = conn.execute("SELECT * FROM item_tags WHERE item_id = ?", (item["id"],)).fetchall()
    assert len(item_tags) == 3
    assert all(it["source"] == "pipeline" for it in item_tags)

    # FTS updated with summary text.
    fts_row = conn.execute(
        "SELECT summary FROM items_fts WHERE rowid = ?", (item["id"],)
    ).fetchone()
    assert "LoRA fine-tuning" in fts_row["summary"]

    conn.close()


async def test_summarize_item_no_openai_key(tmp_path: Path) -> None:
    """Without an OpenAI key and no injected agent, item is marked as error."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]
    _write_transcript_file(settings.content_dir)

    ok = await summarize_item(item["id"], conn, settings)  # type: ignore[arg-type]

    assert ok is False

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1

    meta = json.loads(row["metadata"])
    assert "OpenAI API key not configured" in meta["last_error"]

    conn.close()


async def test_summarize_item_llm_failure(tmp_path: Path) -> None:
    """Agent exception → returns False, retry_count incremented."""

    async def failing_handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("LLM exploded")

    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]
    _write_transcript_file(settings.content_dir)

    agent = create_summarizer_agent(model="test")
    with agent.override(model=FunctionModel(failing_handler)):
        ok = await summarize_item(item["id"], conn, settings, agent=agent)  # type: ignore[arg-type]

    assert ok is False

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1

    meta = json.loads(row["metadata"])
    assert "LLM exploded" in meta["last_error"]

    conn.close()


def test_normalize_slug() -> None:
    """Slug normalization: spaces → hyphens, uppercase → lowercase, specials removed."""
    assert _normalize_slug("LoRA") == "lora"
    assert _normalize_slug("fine tuning") == "fine-tuning"
    assert _normalize_slug("Apache Iceberg") == "apache-iceberg"
    assert _normalize_slug("C++") == "c"
    assert _normalize_slug("retrieval-augmented-generation") == "retrieval-augmented-generation"
    assert _normalize_slug("  spaces  ") == "spaces"
    assert _normalize_slug("a--b") == "a-b"
    assert _normalize_slug("under_score") == "under-score"


def test_insert_tags_idempotent(tmp_path: Path) -> None:
    """Calling _insert_tags twice with the same topics creates no duplicates."""
    conn = _make_conn(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    topics = ["lora", "fine-tuning"]
    _insert_tags(conn, item["id"], topics)  # type: ignore[arg-type]
    _insert_tags(conn, item["id"], topics)  # type: ignore[arg-type]

    tags = conn.execute("SELECT * FROM tags").fetchall()
    assert len(tags) == 2

    item_tags = conn.execute("SELECT * FROM item_tags WHERE item_id = ?", (item["id"],)).fetchall()
    assert len(item_tags) == 2

    conn.close()


def test_read_transcript_strips_front_matter(tmp_path: Path) -> None:
    """Transcript file with YAML front matter returns only the body text."""
    content_dir = tmp_path / "content"
    _write_transcript_file(content_dir)
    text = _read_transcript(content_dir, "transcripts/youtube/vid1.md")
    assert "---" not in text
    assert "item_id" not in text
    assert "Welcome to the video" in text


def test_read_transcript_no_front_matter(tmp_path: Path) -> None:
    """Transcript file without YAML front matter returns the full text."""
    content_dir = tmp_path / "content"
    transcript_dir = content_dir / "transcripts" / "youtube"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "vid1.md").write_text(
        "Just raw transcript text with no front matter.\n",
        encoding="utf-8",
    )
    text = _read_transcript(content_dir, "transcripts/youtube/vid1.md")
    assert text == "Just raw transcript text with no front matter."
