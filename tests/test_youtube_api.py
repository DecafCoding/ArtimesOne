"""Tests for artimesone.collectors.youtube.api — YouTube Data API client."""

from __future__ import annotations

import httpx
import respx

from artimesone.collectors.youtube.api import (
    YouTubeDataAPIClient,
    parse_iso8601_duration,
)

# ---------------------------------------------------------------------------
# parse_iso8601_duration
# ---------------------------------------------------------------------------


def test_parse_full_duration() -> None:
    assert parse_iso8601_duration("PT1H2M3S") == 3723


def test_parse_minutes_seconds() -> None:
    assert parse_iso8601_duration("PT5M30S") == 330


def test_parse_seconds_only() -> None:
    assert parse_iso8601_duration("PT30S") == 30


def test_parse_zero_returns_none() -> None:
    """PT0S (livestreams/premieres) returns None."""
    assert parse_iso8601_duration("PT0S") is None


def test_parse_bogus_returns_none() -> None:
    assert parse_iso8601_duration("BOGUS") is None


def test_parse_empty_returns_none() -> None:
    assert parse_iso8601_duration("") is None


# ---------------------------------------------------------------------------
# YouTubeDataAPIClient
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_uploads_playlist_id() -> None:
    respx.get("https://www.googleapis.com/youtube/v3/channels").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]},
        )
    )
    client = YouTubeDataAPIClient(api_key="fake")
    try:
        result = await client.get_uploads_playlist_id("UCtest")
        assert result == "UUtest"
    finally:
        await client.close()


@respx.mock
async def test_missing_channel_returns_none() -> None:
    respx.get("https://www.googleapis.com/youtube/v3/channels").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    client = YouTubeDataAPIClient(api_key="fake")
    try:
        result = await client.get_uploads_playlist_id("UCnone")
        assert result is None
    finally:
        await client.close()


@respx.mock
async def test_list_playlist_items() -> None:
    respx.get("https://www.googleapis.com/youtube/v3/playlistItems").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"contentDetails": {"videoId": "vid1"}, "snippet": {}},
                    {"contentDetails": {"videoId": "vid2"}, "snippet": {}},
                ]
            },
        )
    )
    client = YouTubeDataAPIClient(api_key="fake")
    try:
        items = await client.list_playlist_items("UUtest")
        assert len(items) == 2
    finally:
        await client.close()


@respx.mock
async def test_get_video_details_batches() -> None:
    """Passing 60 video IDs results in two API calls (50 + 10)."""
    call_count = 0

    def _side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        ids = request.url.params.get("id", "").split(",")
        items = [{"id": vid_id} for vid_id in ids]
        return httpx.Response(200, json={"items": items})

    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(side_effect=_side_effect)
    client = YouTubeDataAPIClient(api_key="fake")
    try:
        video_ids = [f"vid{i}" for i in range(60)]
        result = await client.get_video_details(video_ids)
        assert len(result) == 60
        assert call_count == 2
    finally:
        await client.close()
