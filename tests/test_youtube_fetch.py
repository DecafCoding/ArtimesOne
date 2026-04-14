"""Tests for YouTubeChannelCollector.fetch() — fully offline via respx."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import respx

from artimesone.collectors.youtube.collector import YouTubeChannelCollector
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations

_APIFY_SYNC_URL = (
    "https://api.apify.com/v2/acts/streamers~youtube-scraper/run-sync-get-dataset-items"
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


def _make_settings(
    tmp_path: Path,
    *,
    apify_token: str | None = "fake-token",
) -> Settings:
    # apify_token has validation_alias="APIFY_TOKEN" so we must use the alias
    # name as the keyword; the field name is silently ignored without
    # populate_by_name=True.
    kwargs: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "content_dir": tmp_path / "content",
        "youtube_api_key": "fake",
        "_env_file": None,
    }
    if apify_token is not None:
        kwargs["APIFY_TOKEN"] = apify_token
    return Settings(**kwargs)  # type: ignore[arg-type]


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "data" / "test.db"
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


def _seed_item(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    external_id: str = "vid1",
    title: str = "Test Video",
    status: str = "discovered",
    retry_count: int = 0,
) -> dict[str, object]:
    """Insert an item row and return it as a dict."""
    now = "2026-01-01T00:00:00+00:00"
    url = f"https://www.youtube.com/watch?v={external_id}"
    metadata = json.dumps({"duration_seconds": 600, "thumbnail_url": None, "description": ""})
    conn.execute(
        """
        INSERT INTO items
            (source_id, external_id, title, url, published_at, fetched_at,
             metadata, status, retry_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, external_id, title, url, now, now, metadata, status, retry_count, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE external_id = ?", (external_id,)).fetchone()
    return dict(row)


@respx.mock
async def test_fetch_success(tmp_path: Path) -> None:
    """Successful fetch writes transcript file and updates item status."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]))

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is True
    assert result.error is None

    # Item status updated.
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "transcribed"
    assert row["transcript_path"] == "transcripts/youtube/vid1.md"

    # Transcript file exists with correct content.
    transcript_path = settings.content_dir / "transcripts" / "youtube" / "vid1.md"
    assert transcript_path.exists()
    text = transcript_path.read_text(encoding="utf-8")
    assert "---" in text
    assert "external_id: vid1" in text
    assert 'title: "Test Video"' in text
    assert "Welcome to the video" in text
    assert "Today we discuss testing" in text

    # Metadata merged.
    meta = json.loads(row["metadata"])
    assert meta["description"] == "A great video about testing."
    assert "view_count" not in meta  # moved to first-class column

    conn.close()


async def test_fetch_no_apify_token(tmp_path: Path) -> None:
    """Without an Apify token, fetch returns an error without making API calls."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, apify_token=None)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is False
    assert result.error == "Apify token not configured"

    # Item status unchanged — no DB update on missing token.
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "discovered"
    conn.close()


@respx.mock
async def test_fetch_no_transcript_available(tmp_path: Path) -> None:
    """Apify returns no subtitles — item marked as error with retry_count incremented."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    apify_item = {**_SAMPLE_APIFY_ITEM, "subtitles": None}
    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[apify_item]))

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is False
    assert "No transcript available" in (result.error or "")

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1

    meta = json.loads(row["metadata"])
    assert meta["last_error"] == "No transcript available"
    conn.close()


@respx.mock
async def test_fetch_apify_error(tmp_path: Path) -> None:
    """Apify API failure marks item as error with retry_count incremented."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is False
    assert result.error is not None

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "error"
    assert row["retry_count"] == 1

    meta = json.loads(row["metadata"])
    assert "last_error" in meta
    conn.close()


@respx.mock
async def test_fetch_creates_directory(tmp_path: Path) -> None:
    """Fetch creates the content/transcripts/youtube/ directory if it doesn't exist."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(conn, source["id"])  # type: ignore[arg-type]

    # Ensure the directory does NOT exist yet.
    transcript_dir = settings.content_dir / "transcripts" / "youtube"
    assert not transcript_dir.exists()

    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]))

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is True
    assert transcript_dir.exists()
    assert (transcript_dir / "vid1.md").exists()
    conn.close()


@respx.mock
async def test_fetch_escapes_title_in_yaml(tmp_path: Path) -> None:
    """Titles with special YAML characters are properly escaped in front matter."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)
    item = _seed_item(
        conn,
        source["id"],
        title='Video with "quotes" and backslash\\',  # type: ignore[arg-type]
    )

    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]))

    collector = YouTubeChannelCollector()
    result = await collector.fetch(item, conn, settings)  # type: ignore[arg-type]

    assert result.success is True

    transcript_path = settings.content_dir / "transcripts" / "youtube" / "vid1.md"
    text = transcript_path.read_text(encoding="utf-8")
    # Quotes and backslashes should be escaped inside the YAML double-quoted value.
    assert r'title: "Video with \"quotes\" and backslash\\"' in text
    conn.close()
