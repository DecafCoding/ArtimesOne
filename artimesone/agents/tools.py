"""Chat agent tools — the 17 functions that give the agent corpus access.

Organized into three tiers per plan section 6:
- **Read tools** (11): query items, transcripts, topics, sources, stats,
  rollups, lists
- **Write tools** (3): create/update rollups, add tags
- **Source-management tools** (3): add/enable/disable sources

All tools receive ``RunContext[ChatDeps]`` for DB and settings access.
Read tools return Pydantic models; write tools return ``str`` or ``int``.
No tool raises exceptions — errors are caught and returned as descriptive
strings so the LLM can report them to the user.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent, RunContext

from artimesone.agents.chat import ChatDeps
from artimesone.agents.models import (
    CorpusStats,
    ItemDetail,
    ItemSummary,
    ListDetail,
    ListInfo,
    RollupDetail,
    RollupSummary,
    SourceInfo,
    TopicInfo,
)
from artimesone.lists import get_list_by_id, get_lists_by_kind
from artimesone.web.filters_sql import build_visibility_filter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_slug(tag: str) -> str:
    """Normalize a topic tag to a slug: lowercase, hyphens, no special chars.

    Mirrors ``_normalize_slug`` in ``pipeline/summarize.py``.
    """
    slug = tag.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _slugify_title(title: str) -> str:
    """Turn a rollup title into a filesystem-safe slug."""
    slug = title.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")[:60]


def _escape_yaml(value: str) -> str:
    """Escape special characters for a YAML double-quoted string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_metadata(raw: str | None) -> dict[str, object]:
    """Safely parse a JSON metadata string."""
    if not raw:
        return {}
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
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
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip() or None


def _fetch_item_topics(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """Return topic names for a single item."""
    rows = conn.execute(
        """
        SELECT t.name
        FROM item_tags it JOIN tags t ON t.id = it.tag_id
        WHERE it.item_id = ?
        ORDER BY t.name
        """,
        (item_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _build_item_summary(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    content_dir: Path,
    *,
    snippet: str | None = None,
) -> ItemSummary:
    """Build an ItemSummary from an items+sources joined row."""
    summary_text = snippet or _read_summary_text(content_dir, row["summary_path"])
    return ItemSummary(
        id=row["id"],
        title=row["title"],
        url=row["url"],
        published_at=row["published_at"],
        source_name=row["source_name"],
        summary_snippet=summary_text,
        topics=_fetch_item_topics(conn, row["id"]),
    )


def _escape_fts_query(query: str) -> str:
    """Escape an FTS5 query to avoid syntax errors from special characters.

    Wraps each whitespace-separated token in double quotes so characters
    like ``*``, ``"``, and ``(`` are treated as literals.
    """
    tokens = query.split()
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens if t.strip()]
    return " ".join(escaped)


def _insert_or_get_tag(conn: sqlite3.Connection, tag: str) -> int | None:
    """Insert a tag if it doesn't exist and return its id."""
    slug = _normalize_slug(tag)
    if not slug:
        return None
    name = tag.strip()
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO tags (slug, name, created_at) VALUES (?, ?, ?)",
        (slug, name, now_iso),
    )
    row = conn.execute("SELECT id FROM tags WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    tag_id: int = row["id"]
    return tag_id


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

# Read tools ------------------------------------------------------------------


async def search_items(
    ctx: RunContext[ChatDeps],
    query: str,
    topic: str | None = None,
    source_type: str | None = None,
    limit: int = 20,
) -> list[ItemSummary]:
    """Search items by keyword using full-text search. Returns ranked results
    with summary snippets. Optionally filter by topic slug or source type."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    escaped_query = _escape_fts_query(query)
    if not escaped_query:
        return []

    sql = """
        SELECT i.id, i.title, i.url, i.published_at, i.summary_path,
               i.metadata, i.status,
               s.name AS source_name,
               snippet(items_fts, 1, '<b>', '</b>', '...', 30) AS fts_snippet
        FROM items_fts
        JOIN items i ON i.id = items_fts.rowid
        JOIN sources s ON s.id = i.source_id
    """
    params: list[object] = []
    joins: list[str] = []
    wheres: list[str] = ["items_fts MATCH ?", build_visibility_filter("i")]
    params.append(escaped_query)

    if topic:
        joins.append("JOIN item_tags it ON it.item_id = i.id")
        joins.append("JOIN tags t ON t.id = it.tag_id")
        wheres.append("t.slug = ?")
        params.append(topic)

    if source_type:
        wheres.append("s.type = ?")
        params.append(source_type)

    sql += " ".join(joins)
    sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("FTS query failed: %s", exc)
        return []

    return [_build_item_summary(row, conn, content_dir, snippet=row["fts_snippet"]) for row in rows]


async def get_item(ctx: RunContext[ChatDeps], item_id: int) -> ItemDetail | str:
    """Get full details for a single item including summary text and metadata.
    Does NOT include the transcript — use get_transcript for that."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    row = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.published_at, i.status,
               i.metadata, i.summary_path,
               s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return f"Item {item_id} not found."

    metadata = _parse_metadata(row["metadata"])
    summary_text = _read_summary_text(content_dir, row["summary_path"])

    return ItemDetail(
        id=row["id"],
        title=row["title"],
        url=row["url"],
        published_at=row["published_at"],
        source_name=row["source_name"],
        summary=summary_text,
        topics=_fetch_item_topics(conn, row["id"]),
        status=row["status"],
        duration_seconds=metadata.get("duration_seconds"),  # type: ignore[arg-type]
        thumbnail_url=metadata.get("thumbnail_url"),  # type: ignore[arg-type]
    )


async def get_transcript(ctx: RunContext[ChatDeps], item_id: int) -> str:
    """Get the raw transcript text for an item. This is the full text, which
    can be long — use get_item first to check if the item is relevant."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    row = conn.execute("SELECT transcript_path FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return f"Item {item_id} not found."
    if not row["transcript_path"]:
        return f"Item {item_id} has no transcript."

    full_path = content_dir / row["transcript_path"]
    if not full_path.exists():
        return f"Transcript file not found for item {item_id}."

    text: str = full_path.read_text(encoding="utf-8")
    # Strip YAML front matter.
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


async def list_recent_items(
    ctx: RunContext[ChatDeps],
    topic: str | None = None,
    source_type: str | None = None,
    days: int = 7,
    limit: int = 20,
) -> list[ItemSummary]:
    """List recent items from the last N days. Optionally filter by topic
    slug or source type."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    sql = """
        SELECT i.id, i.title, i.url, i.published_at, i.summary_path,
               i.metadata, i.status,
               s.name AS source_name
        FROM items i
        JOIN sources s ON s.id = i.source_id
    """
    params: list[object] = []
    joins: list[str] = []
    wheres: list[str] = ["i.created_at >= ?", build_visibility_filter("i")]
    params.append(cutoff)

    if topic:
        joins.append("JOIN item_tags it ON it.item_id = i.id")
        joins.append("JOIN tags t ON t.id = it.tag_id")
        wheres.append("t.slug = ?")
        params.append(topic)

    if source_type:
        wheres.append("s.type = ?")
        params.append(source_type)

    sql += " ".join(joins)
    sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY COALESCE(i.published_at, i.created_at) DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [_build_item_summary(row, conn, content_dir) for row in rows]


async def list_topics(ctx: RunContext[ChatDeps], min_items: int = 1) -> list[TopicInfo]:
    """List all topics with their item counts. Filter by minimum item count."""
    conn = ctx.deps.conn
    rows = conn.execute(
        """
        SELECT t.slug, t.name, COUNT(it.item_id) AS item_count
        FROM tags t
        JOIN item_tags it ON it.tag_id = t.id
        GROUP BY t.id
        HAVING item_count >= ?
        ORDER BY item_count DESC
        """,
        (min_items,),
    ).fetchall()
    return [TopicInfo(slug=r["slug"], name=r["name"], item_count=r["item_count"]) for r in rows]


async def list_sources(ctx: RunContext[ChatDeps]) -> list[SourceInfo]:
    """List all registered content sources with their enabled state."""
    conn = ctx.deps.conn
    rows = conn.execute(
        "SELECT id, type, external_id, name, enabled FROM sources ORDER BY id"
    ).fetchall()
    return [
        SourceInfo(
            id=r["id"],
            type=r["type"],
            external_id=r["external_id"],
            name=r["name"],
            enabled=bool(r["enabled"]),
        )
        for r in rows
    ]


async def get_stats(ctx: RunContext[ChatDeps]) -> CorpusStats:
    """Get aggregate statistics about the entire corpus."""
    conn = ctx.deps.conn

    total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    total_topics = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]

    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM items GROUP BY status"
    ).fetchall()
    items_by_status = {r["status"]: r["cnt"] for r in status_rows}

    last_run_row = conn.execute(
        "SELECT started_at FROM collection_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    last_run = last_run_row["started_at"] if last_run_row else None

    return CorpusStats(
        total_items=total_items,
        total_sources=total_sources,
        total_topics=total_topics,
        items_by_status=items_by_status,
        last_collection_run=last_run,
    )


async def list_rollups(
    ctx: RunContext[ChatDeps], topic: str | None = None, limit: int = 20
) -> list[RollupSummary]:
    """List rollup documents, optionally filtered by topic slug."""
    conn = ctx.deps.conn

    if topic:
        rows = conn.execute(
            """
            SELECT r.id, r.title, r.generated_by, r.created_at
            FROM rollups r
            JOIN rollup_tags rt ON rt.rollup_id = r.id
            JOIN tags t ON t.id = rt.tag_id
            WHERE t.slug = ?
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (topic, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, generated_by, created_at FROM rollups "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    result: list[RollupSummary] = []
    for r in rows:
        # Fetch topics for this rollup.
        tag_rows = conn.execute(
            """
            SELECT t.name FROM rollup_tags rt
            JOIN tags t ON t.id = rt.tag_id
            WHERE rt.rollup_id = ?
            ORDER BY t.name
            """,
            (r["id"],),
        ).fetchall()
        result.append(
            RollupSummary(
                id=r["id"],
                title=r["title"],
                topics=[t["name"] for t in tag_rows],
                generated_by=r["generated_by"],
                created_at=r["created_at"],
            )
        )
    return result


async def get_rollup(ctx: RunContext[ChatDeps], rollup_id: int) -> RollupDetail | str:
    """Get full rollup details including body text and cited source items."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir

    row = conn.execute(
        "SELECT id, title, file_path, generated_by, created_at FROM rollups WHERE id = ?",
        (rollup_id,),
    ).fetchone()
    if row is None:
        return f"Rollup {rollup_id} not found."

    # Read body from markdown file.
    body = _read_summary_text(content_dir, row["file_path"]) or ""

    # Fetch topics.
    tag_rows = conn.execute(
        """
        SELECT t.name FROM rollup_tags rt
        JOIN tags t ON t.id = rt.tag_id
        WHERE rt.rollup_id = ?
        ORDER BY t.name
        """,
        (rollup_id,),
    ).fetchall()

    # Fetch cited source items.
    item_rows = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.published_at, i.summary_path,
               i.metadata, i.status,
               s.name AS source_name
        FROM rollup_items ri
        JOIN items i ON i.id = ri.item_id
        JOIN sources s ON s.id = i.source_id
        WHERE ri.rollup_id = ?
        ORDER BY i.published_at DESC
        """,
        (rollup_id,),
    ).fetchall()

    source_items = [_build_item_summary(ir, conn, content_dir) for ir in item_rows]

    return RollupDetail(
        id=row["id"],
        title=row["title"],
        topics=[t["name"] for t in tag_rows],
        generated_by=row["generated_by"],
        created_at=row["created_at"],
        body=body,
        source_items=source_items,
    )


async def get_lists(ctx: RunContext[ChatDeps], kind: str | None = None) -> list[ListInfo]:
    """List user-curated lists (libraries and projects) with item counts.

    Pass ``kind='library'`` or ``kind='project'`` to filter; omit for all."""
    conn = ctx.deps.conn
    if kind not in (None, "library", "project"):
        return []
    rows = get_lists_by_kind(conn, kind)  # type: ignore[arg-type]
    return [
        ListInfo(
            id=r["id"],
            name=r["name"],
            kind=r["kind"],
            item_count=r["item_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def get_list(ctx: RunContext[ChatDeps], list_id: int) -> ListDetail | str:
    """Get list metadata plus its member items. Use this to answer questions
    like 'what's in my AI Skills project?'"""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir

    row = get_list_by_id(conn, list_id)
    if row is None:
        return f"List {list_id} not found."

    item_rows = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.published_at, i.summary_path,
               i.metadata, i.status,
               s.name AS source_name
        FROM list_items li
        JOIN items i ON i.id = li.item_id
        JOIN sources s ON s.id = i.source_id
        WHERE li.list_id = ?
        ORDER BY li.added_at DESC
        """,
        (list_id,),
    ).fetchall()

    items = [_build_item_summary(ir, conn, content_dir) for ir in item_rows]

    return ListDetail(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        items=items,
    )


# Write tools -----------------------------------------------------------------


async def create_rollup(
    ctx: RunContext[ChatDeps],
    title: str,
    body: str,
    topics: list[str],
    source_item_ids: list[int],
) -> int | str:
    """Create a new rollup document with the given title, body, topics, and
    cited source item IDs. Returns the new rollup ID on success."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    now_iso = datetime.now(UTC).isoformat()

    try:
        cursor = conn.execute(
            """
            INSERT INTO rollups (title, file_path, generated_by, generating_prompt,
                                 created_at, updated_at)
            VALUES (?, '', 'chat_agent', NULL, ?, ?)
            """,
            (title, now_iso, now_iso),
        )
        rollup_id: int = cursor.lastrowid  # type: ignore[assignment]

        # Write markdown file.
        slug = _slugify_title(title) or "rollup"
        file_rel = f"rollups/{rollup_id}-{slug}.md"
        file_path = content_dir / file_rel
        file_path.parent.mkdir(parents=True, exist_ok=True)

        topics_json = json.dumps(topics)
        front_matter = (
            "---\n"
            f"rollup_id: {rollup_id}\n"
            f'title: "{_escape_yaml(title)}"\n'
            f"generated_by: chat_agent\n"
            f"created_at: {now_iso}\n"
            f"topics: {topics_json}\n"
            "---\n\n"
        )
        file_path.write_text(front_matter + body, encoding="utf-8")

        # Update the file_path on the rollup row.
        conn.execute("UPDATE rollups SET file_path = ? WHERE id = ?", (file_rel, rollup_id))

        # Insert tags and rollup_tags.
        for topic in topics:
            tag_id = _insert_or_get_tag(conn, topic)
            if tag_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO rollup_tags (rollup_id, tag_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (rollup_id, tag_id, now_iso),
                )

        # Insert rollup_items.
        for item_id in source_item_ids:
            conn.execute(
                "INSERT OR IGNORE INTO rollup_items (rollup_id, item_id, created_at) "
                "VALUES (?, ?, ?)",
                (rollup_id, item_id, now_iso),
            )

        conn.commit()
        return rollup_id

    except Exception as exc:
        logger.exception("create_rollup failed")
        return f"Failed to create rollup: {exc}"


async def update_rollup(
    ctx: RunContext[ChatDeps],
    rollup_id: int,
    title: str | None = None,
    body: str | None = None,
    topics: list[str] | None = None,
) -> str:
    """Update an existing rollup. Pass only the fields you want to change."""
    conn = ctx.deps.conn
    content_dir = ctx.deps.settings.content_dir
    now_iso = datetime.now(UTC).isoformat()

    row = conn.execute(
        "SELECT id, title, file_path FROM rollups WHERE id = ?", (rollup_id,)
    ).fetchone()
    if row is None:
        return f"Rollup {rollup_id} not found."

    try:
        if title is not None:
            conn.execute(
                "UPDATE rollups SET title = ?, updated_at = ? WHERE id = ?",
                (title, now_iso, rollup_id),
            )

        # Rewrite the markdown file if title or body changed.
        if title is not None or body is not None:
            current_title = title if title is not None else row["title"]
            file_rel = row["file_path"]
            if not file_rel:
                slug = _slugify_title(current_title) or "rollup"
                file_rel = f"rollups/{rollup_id}-{slug}.md"
                conn.execute(
                    "UPDATE rollups SET file_path = ? WHERE id = ?",
                    (file_rel, rollup_id),
                )

            file_path = content_dir / file_rel
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if body is None:
                body = _read_summary_text(content_dir, file_rel) or ""

            # Fetch current topics for front matter.
            tag_rows = conn.execute(
                """
                SELECT t.name FROM rollup_tags rt
                JOIN tags t ON t.id = rt.tag_id
                WHERE rt.rollup_id = ?
                """,
                (rollup_id,),
            ).fetchall()
            current_topics = [t["name"] for t in tag_rows]
            topics_json = json.dumps(topics if topics is not None else current_topics)

            front_matter = (
                "---\n"
                f"rollup_id: {rollup_id}\n"
                f'title: "{_escape_yaml(current_title)}"\n'
                f"generated_by: chat_agent\n"
                f"updated_at: {now_iso}\n"
                f"topics: {topics_json}\n"
                "---\n\n"
            )
            file_path.write_text(front_matter + body, encoding="utf-8")

        # Replace rollup_tags if topics changed.
        if topics is not None:
            conn.execute("DELETE FROM rollup_tags WHERE rollup_id = ?", (rollup_id,))
            for topic in topics:
                tag_id = _insert_or_get_tag(conn, topic)
                if tag_id is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO rollup_tags "
                        "(rollup_id, tag_id, created_at) VALUES (?, ?, ?)",
                        (rollup_id, tag_id, now_iso),
                    )

        conn.execute("UPDATE rollups SET updated_at = ? WHERE id = ?", (now_iso, rollup_id))
        conn.commit()
        return f"Rollup {rollup_id} updated."

    except Exception as exc:
        logger.exception("update_rollup failed")
        return f"Failed to update rollup: {exc}"


async def add_tag_to_item(ctx: RunContext[ChatDeps], item_id: int, tag: str) -> str:
    """Add a topic tag to an item. Idempotent — duplicates are no-ops."""
    conn = ctx.deps.conn
    now_iso = datetime.now(UTC).isoformat()

    item_row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item_row is None:
        return f"Item {item_id} not found."

    tag_id = _insert_or_get_tag(conn, tag)
    if tag_id is None:
        return f"Invalid tag: {tag!r}"

    conn.execute(
        "INSERT OR IGNORE INTO item_tags (item_id, tag_id, source, created_at) "
        "VALUES (?, ?, 'agent', ?)",
        (item_id, tag_id, now_iso),
    )
    conn.commit()
    slug = _normalize_slug(tag)
    return f"Tag '{slug}' added to item {item_id}."


# Source-management tools -----------------------------------------------------


async def add_source(
    ctx: RunContext[ChatDeps],
    type: str,
    external_id: str,
    name: str,
) -> int | str:
    """Register a new content source (auto-enabled). Returns the new source ID."""
    conn = ctx.deps.conn
    settings = ctx.deps.settings
    now_iso = datetime.now(UTC).isoformat()
    config = json.dumps({"channel_id": external_id})

    try:
        cursor = conn.execute(
            """
            INSERT INTO sources (type, external_id, name, config, enabled,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (type, external_id, name, config, now_iso, now_iso),
        )
        conn.commit()
        source_id: int = cursor.lastrowid  # type: ignore[assignment]
    except sqlite3.IntegrityError:
        return f"A source with type={type!r} and external_id={external_id!r} already exists."

    # Reload scheduler jobs so the new source starts collecting.
    if ctx.deps.scheduler is not None:
        from artimesone.scheduler import reload_jobs

        reload_jobs(ctx.deps.scheduler, settings)

    return source_id


async def enable_source(ctx: RunContext[ChatDeps], source_id: int) -> str:
    """Enable a content source so it resumes scheduled collection."""
    conn = ctx.deps.conn
    settings = ctx.deps.settings
    now_iso = datetime.now(UTC).isoformat()

    row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return f"Source {source_id} not found."

    conn.execute(
        "UPDATE sources SET enabled = 1, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()

    if ctx.deps.scheduler is not None:
        from artimesone.scheduler import reload_jobs

        reload_jobs(ctx.deps.scheduler, settings)

    return f"Source {source_id} enabled."


async def disable_source(ctx: RunContext[ChatDeps], source_id: int) -> str:
    """Disable a content source so it stops scheduled collection."""
    conn = ctx.deps.conn
    settings = ctx.deps.settings
    now_iso = datetime.now(UTC).isoformat()

    row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return f"Source {source_id} not found."

    conn.execute(
        "UPDATE sources SET enabled = 0, updated_at = ? WHERE id = ?",
        (now_iso, source_id),
    )
    conn.commit()

    if ctx.deps.scheduler is not None:
        from artimesone.scheduler import reload_jobs

        reload_jobs(ctx.deps.scheduler, settings)

    return f"Source {source_id} disabled."


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tools(agent: Agent[ChatDeps, str]) -> None:
    """Register all 17 tools on the given agent instance."""
    # Read tools
    agent.tool(search_items)
    agent.tool(get_item)
    agent.tool(get_transcript)
    agent.tool(list_recent_items)
    agent.tool(list_topics)
    agent.tool(list_sources)
    agent.tool(get_stats)
    agent.tool(list_rollups)
    agent.tool(get_rollup)
    agent.tool(get_lists)
    agent.tool(get_list)

    # Write tools
    agent.tool(create_rollup)
    agent.tool(update_rollup)
    agent.tool(add_tag_to_item)

    # Source-management tools
    agent.tool(add_source)
    agent.tool(enable_source)
    agent.tool(disable_source)
