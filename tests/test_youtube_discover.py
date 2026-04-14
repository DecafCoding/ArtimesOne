"""Tests for YouTubeChannelCollector.discover() — fully offline via respx."""

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

_YT_BASE = "https://www.googleapis.com/youtube/v3"


def _make_settings(tmp_path: Path, *, api_key: str | None = "fake") -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        content_dir=tmp_path / "content",
        youtube_api_key=api_key,
        _env_file=None,  # type: ignore[call-arg]
    )


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


def _mock_youtube_api(
    playlist_video_ids: list[str],
    video_details: dict[str, dict[str, object]],
) -> None:
    """Set up respx mocks for the standard discover() call sequence."""
    # channels.list → uploads playlist ID
    respx.get(f"{_YT_BASE}/channels").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]},
        )
    )

    # playlistItems.list → video IDs
    playlist_items = [
        {"contentDetails": {"videoId": vid}, "snippet": {}} for vid in playlist_video_ids
    ]
    respx.get(f"{_YT_BASE}/playlistItems").mock(
        return_value=httpx.Response(200, json={"items": playlist_items})
    )

    # videos.list → details with durations
    detail_items = []
    for vid_id, detail in video_details.items():
        detail_items.append({"id": vid_id, **detail})
    respx.get(f"{_YT_BASE}/videos").mock(
        return_value=httpx.Response(200, json={"items": detail_items})
    )


@respx.mock
async def test_discover_new_videos(tmp_path: Path) -> None:
    """First run discovers new videos and inserts them, capturing stats."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    _mock_youtube_api(
        playlist_video_ids=["vid1", "vid2"],
        video_details={
            "vid1": {
                "snippet": {"title": "Video One", "publishedAt": "2026-01-01T00:00:00Z"},
                "contentDetails": {"duration": "PT10M"},
                "statistics": {"viewCount": "12345", "likeCount": "678"},
            },
            "vid2": {
                "snippet": {"title": "Video Two", "publishedAt": "2026-01-01T01:00:00Z"},
                "contentDetails": {"duration": "PT20M"},
                "statistics": {"viewCount": "9"},  # likes hidden
            },
        },
    )

    collector = YouTubeChannelCollector()
    result = await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    assert result.discovered == 2
    assert result.filtered_out == 0
    assert result.error is None

    rows = conn.execute(
        "SELECT external_id, view_count, like_count FROM items "
        "WHERE source_id = ? ORDER BY external_id",
        (source["id"],),
    ).fetchall()
    assert len(rows) == 2
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["vid1"]["view_count"] == 12345
    assert by_id["vid1"]["like_count"] == 678
    assert by_id["vid2"]["view_count"] == 9
    assert by_id["vid2"]["like_count"] is None
    conn.close()


@respx.mock
async def test_discover_stop_at_known(tmp_path: Path) -> None:
    """Second run with same data inserts zero new rows (stop-at-known)."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    video_ids = ["vid1", "vid2"]
    details = {
        "vid1": {
            "snippet": {"title": "Video One", "publishedAt": "2026-01-01T00:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
        },
        "vid2": {
            "snippet": {"title": "Video Two", "publishedAt": "2026-01-01T01:00:00Z"},
            "contentDetails": {"duration": "PT20M"},
        },
    }

    # First run
    _mock_youtube_api(video_ids, details)
    collector = YouTubeChannelCollector()
    await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    # Second run with same mocks
    _mock_youtube_api(video_ids, details)
    result = await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    assert result.discovered == 0
    assert result.filtered_out == 0

    rows = conn.execute("SELECT * FROM items WHERE source_id = ?", (source["id"],)).fetchall()
    assert len(rows) == 2  # no new rows
    conn.close()


@respx.mock
async def test_discover_filters_long_videos(tmp_path: Path) -> None:
    """Videos exceeding max_video_duration_minutes are skipped_too_long."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    _mock_youtube_api(
        playlist_video_ids=["short", "long"],
        video_details={
            "short": {
                "snippet": {"title": "Short", "publishedAt": "2026-01-01T00:00:00Z"},
                "contentDetails": {"duration": "PT30M"},
            },
            "long": {
                "snippet": {"title": "Long", "publishedAt": "2026-01-01T01:00:00Z"},
                "contentDetails": {"duration": "PT2H"},
            },
        },
    )

    collector = YouTubeChannelCollector()
    result = await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    assert result.discovered == 1
    assert result.filtered_out == 1

    long_row = conn.execute("SELECT status FROM items WHERE external_id = 'long'").fetchone()
    assert long_row["status"] == "skipped_too_long"
    conn.close()


@respx.mock
async def test_discover_missing_duration_is_skipped(tmp_path: Path) -> None:
    """A video with no duration is treated as skipped_too_long."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path)
    source = _seed_source(conn)

    _mock_youtube_api(
        playlist_video_ids=["nodur"],
        video_details={
            "nodur": {
                "snippet": {"title": "No Duration", "publishedAt": "2026-01-01T00:00:00Z"},
                "contentDetails": {},
            },
        },
    )

    collector = YouTubeChannelCollector()
    result = await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    assert result.discovered == 0
    assert result.filtered_out == 1

    row = conn.execute("SELECT status FROM items WHERE external_id = 'nodur'").fetchone()
    assert row["status"] == "skipped_too_long"
    conn.close()


async def test_discover_no_api_key(tmp_path: Path) -> None:
    """Without a YouTube API key, discover returns an error with no API calls."""
    conn = _make_conn(tmp_path)
    settings = _make_settings(tmp_path, api_key=None)
    source = _seed_source(conn)

    collector = YouTubeChannelCollector()
    result = await collector.discover(source, conn, settings)  # type: ignore[arg-type]

    assert result.discovered == 0
    assert result.filtered_out == 0
    assert result.error == "YouTube API key not configured"
    conn.close()
