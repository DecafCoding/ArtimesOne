"""Tests for artimesone.collectors.youtube.apify — Apify REST API client."""

from __future__ import annotations

import httpx
import pytest
import respx

from artimesone.collectors.youtube.apify import ApifyClient, ApifyError

_SYNC_URL = "https://api.apify.com/v2/acts/streamers~youtube-scraper/run-sync-get-dataset-items"

# ---------------------------------------------------------------------------
# _parse_srt
# ---------------------------------------------------------------------------


def test_parse_srt_basic() -> None:
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "Hello world\n"
        "\n"
        "2\n"
        "00:00:04,000 --> 00:00:06,000\n"
        "Another line\n"
    )
    assert ApifyClient._parse_srt(srt) == "Hello world\nAnother line"


def test_parse_srt_strips_html_tags() -> None:
    srt = "1\n00:00:01,000 --> 00:00:03,000\n<b>Bold text</b> and <i>italic</i>\n"
    assert ApifyClient._parse_srt(srt) == "Bold text and italic"


def test_parse_srt_empty_input() -> None:
    assert ApifyClient._parse_srt("") == ""


def test_parse_srt_multiline_cue() -> None:
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:03,000\n"
        "Line one\n"
        "Line two\n"
        "\n"
        "2\n"
        "00:00:04,000 --> 00:00:06,000\n"
        "Line three\n"
    )
    assert ApifyClient._parse_srt(srt) == "Line one\nLine two\nLine three"


# ---------------------------------------------------------------------------
# fetch_transcript
# ---------------------------------------------------------------------------

_SAMPLE_ITEM = {
    "subtitles": [
        {
            "srt": (
                "1\n"
                "00:00:01,000 --> 00:00:05,000\n"
                "Welcome to the video\n"
                "\n"
                "2\n"
                "00:00:06,000 --> 00:00:10,000\n"
                "Today we discuss testing\n"
            ),
            "type": "auto_generated",
            "language": "en",
        }
    ],
    "description": "A great video about testing.",
    "duration": 600,
}


@respx.mock
async def test_fetch_transcript_success() -> None:
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_ITEM]))
    client = ApifyClient(token="fake")
    try:
        result = await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
        assert result.transcript == "Welcome to the video\nToday we discuss testing"
        assert result.description == "A great video about testing."
        assert result.duration_seconds == 600
    finally:
        await client.close()


@respx.mock
async def test_fetch_transcript_no_subtitles() -> None:
    item = {**_SAMPLE_ITEM, "subtitles": None}
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(200, json=[item]))
    client = ApifyClient(token="fake")
    try:
        result = await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
        assert result.transcript is None
        assert result.description == "A great video about testing."
    finally:
        await client.close()


@respx.mock
async def test_fetch_transcript_empty_dataset() -> None:
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(200, json=[]))
    client = ApifyClient(token="fake")
    try:
        result = await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
        assert result.transcript is None
        assert result.description is None
        assert result.duration_seconds is None
    finally:
        await client.close()


@respx.mock
async def test_fetch_transcript_api_error() -> None:
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    client = ApifyClient(token="fake")
    try:
        with pytest.raises(ApifyError, match="500"):
            await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
    finally:
        await client.close()


@respx.mock
async def test_fetch_transcript_timeout() -> None:
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(408, text="Request Timeout"))
    client = ApifyClient(token="fake")
    try:
        with pytest.raises(ApifyError, match="408"):
            await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
    finally:
        await client.close()


@respx.mock
async def test_fetch_transcript_empty_srt() -> None:
    """Subtitles present but SRT text is empty → transcript is None."""
    item = {**_SAMPLE_ITEM, "subtitles": [{"srt": "", "type": "auto_generated", "language": "en"}]}
    respx.post(_SYNC_URL).mock(return_value=httpx.Response(200, json=[item]))
    client = ApifyClient(token="fake")
    try:
        result = await client.fetch_transcript("https://www.youtube.com/watch?v=abc")
        assert result.transcript is None
    finally:
        await client.close()
