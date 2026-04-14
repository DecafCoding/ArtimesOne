"""Integration tests for the extended scheduler pipeline: discover → fetch → summarize.

Mocks YouTube Data API, Apify REST API, and the LLM summarizer agent so no
live calls are ever made. Validates retry logic, graceful degradation, and
aggregate run status.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import httpx
import respx
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from artimesone.agents.summarizer import create_summarizer_agent
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations
from artimesone.scheduler import run_source_collection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_YT_BASE = "https://www.googleapis.com/youtube/v3"
_APIFY_SYNC_URL = (
    "https://api.apify.com/v2/acts/streamers~youtube-scraper/run-sync-get-dataset-items"
)

_SUMMARY_JSON = json.dumps(
    {
        "summary": "This video covers LoRA fine-tuning on consumer GPUs.",
        "topics": ["lora", "fine-tuning", "large-language-models"],
    }
)

_SAMPLE_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:05,000\n"
    "Welcome to the video\n"
    "\n"
    "2\n"
    "00:00:06,000 --> 00:00:10,000\n"
    "Today we discuss testing\n"
)

_SAMPLE_APIFY_ITEM = {
    "subtitles": [
        {
            "srt": _SAMPLE_SRT,
            "type": "auto_generated",
            "language": "en",
        }
    ],
    "description": "A great video about testing.",
    "duration": 600,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _stub_handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=_SUMMARY_JSON)])


def _make_settings(
    tmp_path: Path,
    *,
    youtube_api_key: str | None = "fake-yt-key",
    apify_token: str | None = "fake-apify-token",
    openai_api_key: str | None = "fake-openai-key",
) -> Settings:
    kwargs: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "content_dir": tmp_path / "content",
        "_env_file": None,
    }
    if youtube_api_key is not None:
        kwargs["youtube_api_key"] = youtube_api_key
    if apify_token is not None:
        kwargs["APIFY_TOKEN"] = apify_token
    if openai_api_key is not None:
        kwargs["OPENAI_API_KEY"] = openai_api_key
    return Settings(**kwargs)  # type: ignore[arg-type]


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "artimesone.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _seed_source(conn: sqlite3.Connection) -> dict[str, object]:
    """Insert a source row and return it as a dict."""
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


def _mock_youtube_api(video_ids: list[str]) -> None:
    """Set up respx mocks for the YouTube Data API discover() sequence."""
    respx.get(f"{_YT_BASE}/channels").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]},
        )
    )
    playlist_items = [{"contentDetails": {"videoId": vid}, "snippet": {}} for vid in video_ids]
    respx.get(f"{_YT_BASE}/playlistItems").mock(
        return_value=httpx.Response(200, json={"items": playlist_items})
    )
    detail_items = [
        {
            "id": vid,
            "snippet": {"title": f"Video {vid}", "publishedAt": "2026-01-01T00:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
        }
        for vid in video_ids
    ]
    respx.get(f"{_YT_BASE}/videos").mock(
        return_value=httpx.Response(200, json={"items": detail_items})
    )


def _mock_apify_success() -> None:
    """Mock Apify returning a successful transcript."""
    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]))


def _mock_apify_failure() -> None:
    """Mock Apify returning a server error."""
    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))


def _patch_summarizer():
    """Return a context manager that patches create_summarizer_agent with FunctionModel."""
    agent = create_summarizer_agent(model="test")

    def _patched_create(model: str = "test"):
        return agent

    ctx_override = agent.override(model=FunctionModel(_stub_handler))
    return ctx_override, patch(
        "artimesone.pipeline.summarize.create_summarizer_agent", _patched_create
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_full_pipeline_discover_fetch_summarize(tmp_path: Path) -> None:
    """Full run: discover → fetch → summarize. Items end at 'summarized'."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    # Ensure content dirs exist.
    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    _mock_youtube_api(["vid1", "vid2"])
    _mock_apify_success()

    override_ctx, create_patch = _patch_summarizer()
    with override_ctx, create_patch:
        await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    # Items should be summarized.
    rows = conn.execute(
        "SELECT * FROM items WHERE source_id = ? ORDER BY external_id", (source["id"],)
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "summarized"
        assert row["transcript_path"] is not None
        assert row["summary_path"] is not None

    # Transcript files exist.
    for vid in ("vid1", "vid2"):
        assert (settings.content_dir / "transcripts" / "youtube" / f"{vid}.md").exists()

    # Summary files exist.
    for vid in ("vid1", "vid2"):
        assert (settings.content_dir / "summaries" / "youtube" / f"{vid}.md").exists()

    # Tags inserted.
    tags = conn.execute("SELECT slug FROM tags ORDER BY slug").fetchall()
    slugs = [t["slug"] for t in tags]
    assert "lora" in slugs
    assert "fine-tuning" in slugs

    # Collection run is 'success'.
    run = conn.execute(
        "SELECT * FROM collection_runs WHERE source_id = ?", (source["id"],)
    ).fetchone()
    assert run["status"] == "success"
    assert run["items_discovered"] == 2

    conn.close()


@respx.mock
async def test_pipeline_skips_fetch_without_apify_token(tmp_path: Path) -> None:
    """No APIFY_TOKEN → items stay at 'discovered', no errors."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, apify_token=None)
    source = _seed_source(conn)

    _mock_youtube_api(["vid1"])

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid1'").fetchone()
    assert row["status"] == "discovered"
    assert row["transcript_path"] is None

    run = conn.execute(
        "SELECT * FROM collection_runs WHERE source_id = ?", (source["id"],)
    ).fetchone()
    assert run["status"] == "success"

    conn.close()


@respx.mock
async def test_pipeline_skips_summarize_without_openai_key(tmp_path: Path) -> None:
    """Has APIFY_TOKEN but no OPENAI_API_KEY → items reach 'transcribed' but not 'summarized'."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    _mock_youtube_api(["vid1"])
    _mock_apify_success()

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid1'").fetchone()
    assert row["status"] == "transcribed"
    assert row["transcript_path"] is not None
    assert row["summary_path"] is None

    run = conn.execute(
        "SELECT * FROM collection_runs WHERE source_id = ?", (source["id"],)
    ).fetchone()
    assert run["status"] == "success"

    conn.close()


@respx.mock
async def test_retry_failed_fetch(tmp_path: Path) -> None:
    """First run fails Apify → error. Second run retries and succeeds → transcribed."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Run 1: discover succeeds, Apify fails.
    _mock_youtube_api(["vid1"])
    _mock_apify_failure()

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid1'").fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1
    assert row["transcript_path"] is None

    # Run 2: discover finds nothing new (stop-at-known), Apify succeeds for retry.
    respx.reset()
    _mock_youtube_api(["vid1"])  # vid1 is already known → discover returns 0
    _mock_apify_success()

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid1'").fetchone()
    assert row["status"] == "transcribed"
    assert row["transcript_path"] is not None

    conn.close()


@respx.mock
async def test_retry_stops_at_three(tmp_path: Path) -> None:
    """Item with retry_count=3 is skipped on the next run."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)

    # Manually insert an item at retry_count=3 with error status.
    now = "2026-01-01T00:00:00+00:00"
    metadata = json.dumps({"duration_seconds": 600, "last_error": "Previous failure"})
    conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source["id"],
            "vid_maxed",
            "Maxed Out Video",
            "https://www.youtube.com/watch?v=vid_maxed",
            now,
            now,
            metadata,
            "error",
            3,
            now,
            now,
        ),
    )
    conn.commit()

    # Mock discover to return nothing new (the item already exists).
    _mock_youtube_api(["vid_maxed"])
    _mock_apify_success()

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    # Item should still be at error with retry_count=3 — not retried.
    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid_maxed'").fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 3

    conn.close()


@respx.mock
async def test_retry_summarize_failure_requeues(tmp_path: Path) -> None:
    """Summarizer fails on first run → retry_count=1. Second run succeeds → summarized.

    Exercises the Phase 3 (summarize) retry path, complementing the Phase 2
    (fetch) retry coverage in ``test_retry_failed_fetch``.
    """
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Pre-seed a transcribed item with a real transcript file on disk so the
    # summarize phase picks it up without needing discover/fetch.
    transcript_rel = "transcripts/youtube/vid_retry.md"
    (settings.content_dir / transcript_rel).parent.mkdir(parents=True, exist_ok=True)
    (settings.content_dir / transcript_rel).write_text(
        "---\ntitle: Retry Test\n---\n\nA transcript about LoRA training.",
        encoding="utf-8",
    )
    now = "2026-01-01T00:00:00+00:00"
    metadata = json.dumps({"duration_seconds": 600})
    conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, transcript_path, retry_count, created_at, updated_at)
        VALUES (?, 'vid_retry', 'Retry Test', 'https://youtu.be/vid_retry',
                ?, ?, ?, 'transcribed', ?, 0, ?, ?)
        """,
        (source["id"], now, now, metadata, transcript_rel, now, now),
    )
    conn.commit()
    item_id = conn.execute("SELECT id FROM items WHERE external_id = 'vid_retry'").fetchone()["id"]

    # Discover: no new videos. Apify is not needed because nothing is
    # discovered for fetch and the pre-seeded item is already transcribed.
    _mock_youtube_api([])

    agent = create_summarizer_agent(model="test")

    def _patched_create(model: str = "test"):
        return agent

    create_patch = patch("artimesone.pipeline.summarize.create_summarizer_agent", _patched_create)

    async def _raising_handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("simulated summarizer failure")

    # --- Run 1: summarizer raises → item errored, retry_count=1. ---
    with agent.override(model=FunctionModel(_raising_handler)), create_patch:
        await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1
    assert row["summary_path"] is None

    # --- Run 2: summarizer succeeds → item summarized. ---
    respx.reset()
    _mock_youtube_api([])

    with agent.override(model=FunctionModel(_stub_handler)), create_patch:
        await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "summarized"
    assert row["summary_path"] is not None

    conn.close()


@respx.mock
async def test_aggregate_status_partial(tmp_path: Path) -> None:
    """Some items succeed, some fail → collection_runs.status = 'partial'."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    _mock_youtube_api(["vid_ok", "vid_fail"])

    # Apify: first call succeeds, second fails.
    route = respx.post(_APIFY_SYNC_URL)
    route.side_effect = [
        httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]),
        httpx.Response(500, text="Internal Server Error"),
    ]

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    # Check one transcribed, one error.
    rows = conn.execute(
        "SELECT status FROM items WHERE source_id = ? ORDER BY external_id",
        (source["id"],),
    ).fetchall()
    statuses = [r["status"] for r in rows]
    assert "transcribed" in statuses
    assert "error" in statuses

    # Run status should be partial.
    run = conn.execute(
        "SELECT * FROM collection_runs WHERE source_id = ? ORDER BY id DESC LIMIT 1",
        (source["id"],),
    ).fetchone()
    assert run["status"] == "partial"

    conn.close()


@respx.mock
async def test_aggregate_status_success(tmp_path: Path) -> None:
    """All items succeed → collection_runs.status = 'success'."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, openai_api_key=None)
    source = _seed_source(conn)

    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    _mock_youtube_api(["vid1"])
    _mock_apify_success()

    await run_source_collection(source["id"], settings)  # type: ignore[arg-type]

    row = conn.execute("SELECT * FROM items WHERE external_id = 'vid1'").fetchone()
    assert row["status"] == "transcribed"

    run = conn.execute(
        "SELECT * FROM collection_runs WHERE source_id = ? ORDER BY id DESC LIMIT 1",
        (source["id"],),
    ).fetchone()
    assert run["status"] == "success"

    conn.close()
