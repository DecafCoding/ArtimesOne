"""Summarization pipeline: transcribed → summarized.

Reads the transcript markdown file, calls the summarizer agent, writes a
summary markdown file, inserts tags + item_tags rows (source='pipeline'),
refreshes the items_fts row, and updates items.status = 'summarized'.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from artimesone.agents.summarizer import VideoSummary, create_summarizer_agent

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from artimesone.config import Settings

logger = logging.getLogger(__name__)


async def summarize_item(
    item_id: int,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    agent: Agent[None, VideoSummary] | None = None,
) -> bool:
    """Summarize a single transcribed item.

    Returns True on success, False on failure. On failure, the item's
    status is set to 'error' and retry_count is incremented.

    An optional *agent* parameter allows callers (and tests) to inject a
    pre-built agent instance. When omitted the agent is constructed from
    ``settings.summary_model``.
    """
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        logger.warning("Item %s not found", item_id)
        return False

    if row["status"] not in ("transcribed", "error"):
        logger.info("Item %s has status %r, skipping summarize", item_id, row["status"])
        return False

    transcript_path = row["transcript_path"]
    if not transcript_path:
        _mark_error(conn, item_id, "No transcript_path set on item")
        return False

    if settings.openai_api_key is None and agent is None:
        _mark_error(conn, item_id, "OpenAI API key not configured")
        return False

    try:
        transcript_text = _read_transcript(settings.content_dir, transcript_path)
    except FileNotFoundError:
        _mark_error(conn, item_id, f"Transcript file not found: {transcript_path}")
        return False

    if not transcript_text.strip():
        _mark_error(conn, item_id, "Transcript file is empty")
        return False

    if agent is None:
        agent = create_summarizer_agent(model=settings.summary_model)

    try:
        result = await agent.run(transcript_text)
        summary: VideoSummary = result.output
    except Exception as exc:
        logger.exception("Summarizer agent failed for item %s", item_id)
        _mark_error(conn, item_id, str(exc))
        return False

    try:
        metadata: dict[str, object] = json.loads(str(row["metadata"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    summary_rel = _write_summary_md(settings.content_dir, row, summary, metadata)
    _insert_tags(conn, item_id, summary.topics)

    # Refresh FTS with summary text.
    conn.execute(
        "UPDATE items_fts SET summary = ? WHERE rowid = ?",
        (summary.summary, item_id),
    )

    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE items SET status = 'summarized', summary_path = ?, updated_at = ? WHERE id = ?",
        (summary_rel, now_iso, item_id),
    )
    conn.commit()

    logger.info("Summarized item %s", item_id)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_transcript(content_dir: Path, transcript_path: str) -> str:
    """Read transcript text from md file, stripping YAML front matter."""
    full_path = content_dir / transcript_path
    text = full_path.read_text(encoding="utf-8")

    # Strip YAML front matter (--- ... ---).
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]

    return text.strip()


def _escape_yaml(value: str) -> str:
    """Escape special characters for a YAML double-quoted string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_summary_md(
    content_dir: Path,
    item: sqlite3.Row,
    summary: VideoSummary,
    metadata: dict[str, object],
) -> str:
    """Write summary markdown file. Returns the relative path."""
    video_id = item["external_id"]
    summary_rel = f"summaries/youtube/{video_id}.md"
    summary_path = content_dir / summary_rel
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(UTC).isoformat()
    thumbnail_url = metadata.get("thumbnail_url", "")
    duration_seconds = metadata.get("duration_seconds", "")
    topics_yaml = json.dumps(summary.topics)

    front_matter = (
        "---\n"
        f"item_id: {item['id']}\n"
        f"external_id: {video_id}\n"
        f"source: youtube\n"
        f'title: "{_escape_yaml(item["title"])}"\n'
        f"link: {item['url'] or ''}\n"
        f"thumbnail_url: {thumbnail_url or ''}\n"
        f"duration_seconds: {duration_seconds}\n"
        f"published_at: {item['published_at'] or ''}\n"
        f"fetched_at: {item['fetched_at']}\n"
        f"summarized_at: {now_iso}\n"
        f"topics: {topics_yaml}\n"
        "---\n\n"
    )

    summary_path.write_text(front_matter + summary.summary, encoding="utf-8")
    return summary_rel


def _normalize_slug(tag: str) -> str:
    """Normalize a topic tag to a slug: lowercase, hyphens, no special chars."""
    slug = tag.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _insert_tags(
    conn: sqlite3.Connection,
    item_id: int,
    topics: list[str],
) -> None:
    """Insert tags and item_tags rows for the given topics."""
    now_iso = datetime.now(UTC).isoformat()

    for topic in topics:
        slug = _normalize_slug(topic)
        if not slug:
            continue

        # Human-readable name: the original topic string.
        name = topic.strip()

        conn.execute(
            "INSERT OR IGNORE INTO tags (slug, name, created_at) VALUES (?, ?, ?)",
            (slug, name, now_iso),
        )

        tag_row = conn.execute("SELECT id FROM tags WHERE slug = ?", (slug,)).fetchone()
        if tag_row is None:
            continue

        conn.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id, source, created_at) "
            "VALUES (?, ?, 'pipeline', ?)",
            (item_id, tag_row["id"], now_iso),
        )

    conn.commit()


def _mark_error(conn: sqlite3.Connection, item_id: int, error_msg: str) -> None:
    """Mark an item as errored: increment retry_count, store error in metadata."""
    now_iso = datetime.now(UTC).isoformat()
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    try:
        metadata: dict[str, object] = json.loads(str(row["metadata"] if row else "{}"))
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    metadata["last_error"] = error_msg

    conn.execute(
        """
        UPDATE items
        SET status = 'error', retry_count = retry_count + 1,
            metadata = ?, updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata), now_iso, item_id),
    )
    conn.commit()
