"""Thin async wrapper around the YouTube Data API v3.

Covers the three endpoints Phase 1 needs: ``channels.list`` (to resolve the
uploads playlist), ``playlistItems.list`` (to page through recent uploads), and
``videos.list`` (to batch-fetch durations and metadata).

No business logic lives here — the collector (``collector.py``) decides what to
do with the data.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_BASE_URL = "https://www.googleapis.com/youtube/v3"
_TIMEOUT = httpx.Timeout(30.0)
_MAX_VIDEO_IDS_PER_CALL = 50

# ISO 8601 duration: PT[#H][#M][#S]
_ISO8601_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
    re.IGNORECASE,
)


def parse_iso8601_duration(value: str) -> int | None:
    """Parse an ISO 8601 duration string into total seconds.

    Returns ``None`` for unparseable strings *and* for zero-second durations
    (``PT0S``), which YouTube uses for livestreams / premieres where the true
    duration is unknown.
    """
    m = _ISO8601_RE.match(value)
    if m is None:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


class YouTubeAPIError(Exception):
    """Raised when a YouTube Data API call fails."""


class YouTubeDataAPIClient:
    """Async client for the YouTube Data API v3.

    Parameters
    ----------
    api_key:
        YouTube Data API v3 key.
    client:
        Optional pre-configured :class:`httpx.AsyncClient`. If omitted a new
        one is created with a 30-second timeout.
    """

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(base_url=_BASE_URL, timeout=_TIMEOUT)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def get_uploads_playlist_id(self, channel_id: str) -> str | None:
        """Resolve a channel ID to its uploads playlist ID.

        Returns ``None`` when the channel is not found (empty ``items`` list).
        """
        data = await self._get(
            "/channels",
            params={"id": channel_id, "part": "contentDetails"},
        )
        items: list[dict[str, Any]] = data.get("items", [])
        if not items:
            return None
        result: str | None = (
            items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        )
        return result

    async def list_playlist_items(
        self,
        playlist_id: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent items from a playlist (newest first)."""
        data = await self._get(
            "/playlistItems",
            params={
                "playlistId": playlist_id,
                "part": "snippet,contentDetails",
                "maxResults": str(max_results),
            },
        )
        result: list[dict[str, Any]] = data.get("items", [])
        return result

    async def get_video_details(
        self,
        video_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch video details (``contentDetails`` + ``snippet``).

        Automatically splits into chunks of 50 (the API maximum).  Returns a
        dict keyed by video ID.
        """
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(video_ids), _MAX_VIDEO_IDS_PER_CALL):
            batch = video_ids[start : start + _MAX_VIDEO_IDS_PER_CALL]
            data = await self._get(
                "/videos",
                params={
                    "id": ",".join(batch),
                    "part": "contentDetails,snippet",
                },
            )
            for item in data.get("items", []):
                result[item["id"]] = item
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        params["key"] = self._api_key
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise YouTubeAPIError(
                f"YouTube API returned {exc.response.status_code} for {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise YouTubeAPIError(f"YouTube API request failed for {path}") from exc
        return resp.json()  # type: ignore[no-any-return]
