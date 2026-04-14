"""YouTube channel collector — discovery and transcript fetching.

Implements :class:`~artimesone.collectors.Collector` for ``source_type =
"youtube_channel"``.  ``discover()`` resolves the channel's uploads playlist,
pages through recent videos, filters by duration, and inserts rows into
``items`` with ``status='discovered'`` or ``status='skipped_too_long'``.

``fetch()`` calls the Apify ``streamers/youtube-scraper`` actor to retrieve
the transcript for a single discovered video, writes a markdown file with
YAML front matter under ``content/transcripts/youtube/``, and updates the
item to ``status='transcribed'``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from .api import YouTubeAPIError, YouTubeDataAPIClient, parse_iso8601_duration
from .apify import ApifyClient, ApifyError

if TYPE_CHECKING:
    from artimesone.collectors import DiscoverResult, FetchResult, Item, Source
    from artimesone.config import Settings

logger = logging.getLogger(__name__)


def _escape_yaml(value: str) -> str:
    """Escape special characters for a YAML double-quoted string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _pick_thumbnail(snippet: dict[str, object]) -> str | None:
    """Pick the best available thumbnail URL from a video snippet."""
    thumbs = snippet.get("thumbnails", {})
    if not isinstance(thumbs, dict):
        return None
    for key in ("maxres", "standard", "high", "default"):
        entry = thumbs.get(key)
        if isinstance(entry, dict) and "url" in entry:
            return str(entry["url"])
    return None


def _parse_stat_count(value: object) -> int | None:
    """Coerce a YouTube statistics count into int, returning None if absent."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


class YouTubeChannelCollector:
    """Collector for YouTube channels (discovery phase only)."""

    source_type: ClassVar[str] = "youtube_channel"

    async def discover(
        self,
        source: Source,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> DiscoverResult:
        """Discover new videos from a YouTube channel.

        Algorithm (plan section 4, Phase 1):
        1. Bail early if the YouTube API key is not configured.
        2. Resolve the channel's uploads playlist ID.
        3. Page through recent uploads (newest first).
        4. Stop at the first video already known in the DB.
        5. Batch-fetch durations for new videos.
        6. Insert rows: ``discovered`` if within the duration cap,
           ``skipped_too_long`` otherwise (including unknown duration).
        """
        from artimesone.collectors import DiscoverResult

        if settings.youtube_api_key is None:
            return DiscoverResult(0, 0, error="YouTube API key not configured")

        config: dict[str, object] = json.loads(str(source["config"]))
        channel_id = str(config.get("channel_id", ""))
        if not channel_id:
            return DiscoverResult(0, 0, error="Source config missing channel_id")

        client = YouTubeDataAPIClient(api_key=settings.youtube_api_key)
        try:
            return await self._discover(source, conn, settings, client, channel_id)
        except YouTubeAPIError as exc:
            logger.warning("YouTube API error for source %s: %s", source["id"], exc)
            return DiscoverResult(0, 0, error=str(exc))
        finally:
            await client.close()

    async def _discover(
        self,
        source: Source,
        conn: sqlite3.Connection,
        settings: Settings,
        client: YouTubeDataAPIClient,
        channel_id: str,
    ) -> DiscoverResult:
        from artimesone.collectors import DiscoverResult

        uploads_playlist_id = await client.get_uploads_playlist_id(channel_id)
        if uploads_playlist_id is None:
            return DiscoverResult(0, 0, error="Channel not found")

        # Cold-start vs rolling cap: first visit pulls up to initial_video_cap
        # videos; subsequent visits pull at most rolling_video_cap new ones.
        existing_count_row = conn.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?",
            (source["id"],),
        ).fetchone()
        is_cold_start = (existing_count_row[0] or 0) == 0
        max_results = (
            settings.initial_video_cap if is_cold_start else settings.rolling_video_cap
        )
        playlist_items = await client.list_playlist_items(
            uploads_playlist_id, max_results=max_results
        )

        # Extract video IDs in API order (newest first).
        video_ids = [
            item["contentDetails"]["videoId"]
            for item in playlist_items
            if "contentDetails" in item and "videoId" in item["contentDetails"]
        ]
        if not video_ids:
            return DiscoverResult(0, 0)

        # Check which IDs are already known.
        placeholders = ",".join("?" for _ in video_ids)
        rows = conn.execute(
            f"SELECT external_id FROM items WHERE source_id = ? AND external_id IN ({placeholders})",  # noqa: E501
            [source["id"], *video_ids],
        ).fetchall()
        known_ids = {row[0] for row in rows}

        # Stop-at-known: walk newest-first, stop at the first known ID.
        new_video_ids: list[str] = []
        for vid in video_ids:
            if vid in known_ids:
                break
            new_video_ids.append(vid)

        if not new_video_ids:
            return DiscoverResult(0, 0)

        # Batch-fetch details for the new videos.
        details = await client.get_video_details(new_video_ids)

        now_iso = datetime.now(UTC).isoformat()
        max_seconds = settings.max_video_duration_minutes * 60
        discovered = 0
        filtered_out = 0

        for vid in new_video_ids:
            try:
                detail = details.get(vid)
                if detail is None:
                    # Video not returned by the API (private, deleted, etc.)
                    continue

                snippet = detail.get("snippet", {})
                content_details = detail.get("contentDetails", {})
                statistics = detail.get("statistics", {})
                if not isinstance(statistics, dict):
                    statistics = {}
                duration_str = content_details.get("duration", "")
                duration_seconds = parse_iso8601_duration(duration_str) if duration_str else None

                title = str(snippet.get("title", "Untitled"))
                published_at = snippet.get("publishedAt")
                url = f"https://www.youtube.com/watch?v={vid}"
                thumbnail_url = _pick_thumbnail(snippet)
                description = str(snippet.get("description", ""))
                view_count = _parse_stat_count(statistics.get("viewCount"))
                like_count = _parse_stat_count(statistics.get("likeCount"))

                metadata = json.dumps(
                    {
                        "duration_seconds": duration_seconds,
                        "thumbnail_url": thumbnail_url,
                        "description": description[:500],
                    }
                )

                if duration_seconds is not None and duration_seconds <= max_seconds:
                    status = "discovered"
                    discovered += 1
                else:
                    status = "skipped_too_long"
                    filtered_out += 1

                conn.execute(
                    """
                    INSERT OR IGNORE INTO items
                        (source_id, external_id, title, url, published_at, fetched_at,
                         metadata, status, view_count, like_count,
                         retry_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        source["id"],
                        vid,
                        title,
                        url,
                        published_at,
                        now_iso,
                        metadata,
                        status,
                        view_count,
                        like_count,
                        now_iso,
                        now_iso,
                    ),
                )
            except Exception:
                logger.exception("Failed to process video %s, skipping", vid)

        conn.commit()
        return DiscoverResult(discovered, filtered_out)

    async def fetch(
        self,
        item: Item,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> FetchResult:
        """Fetch transcript for a discovered video via Apify.

        1. Bail early if Apify token is not configured.
        2. Call ``ApifyClient.fetch_transcript()`` with the video URL.
        3. Write transcript to ``content/transcripts/youtube/{video_id}.md``
           with YAML front matter.
        4. Update the item row: ``status='transcribed'``, ``transcript_path``
           set, metadata JSON updated with description + duration.
        5. On failure: increment ``retry_count``, set ``status='error'``,
           record error in metadata.
        """
        from artimesone.collectors import FetchResult

        if settings.apify_token is None:
            return FetchResult(success=False, error="Apify token not configured")

        video_id = item["external_id"]
        url = item["url"] or f"https://www.youtube.com/watch?v={video_id}"

        client = ApifyClient(token=settings.apify_token, actor_id=settings.apify_youtube_actor)
        try:
            result = await client.fetch_transcript(url)
        except ApifyError as exc:
            logger.warning("Apify error for item %s: %s", item["id"], exc)
            self._mark_error(conn, item, str(exc))
            return FetchResult(success=False, error=str(exc))
        finally:
            await client.close()

        if result.transcript is None:
            error_msg = "No transcript available"
            self._mark_error(conn, item, error_msg)
            return FetchResult(success=False, error=error_msg)

        # Write transcript markdown file.
        transcript_rel = f"transcripts/youtube/{video_id}.md"
        transcript_path = settings.content_dir / transcript_rel
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        now_iso = datetime.now(UTC).isoformat()
        front_matter = (
            "---\n"
            f"item_id: {item['id']}\n"
            f"external_id: {video_id}\n"
            f"source: youtube\n"
            f'title: "{_escape_yaml(item["title"])}"\n'
            f"published_at: {item.get('published_at', '')}\n"
            f"fetched_at: {now_iso}\n"
            "---\n\n"
        )
        transcript_path.write_text(front_matter + result.transcript, encoding="utf-8")

        # Merge Apify metadata into existing metadata JSON.
        try:
            metadata: dict[str, object] = json.loads(str(item.get("metadata", "{}")))
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if result.description is not None:
            metadata["description"] = result.description[:500]
        if result.duration_seconds is not None:
            metadata["duration_seconds"] = result.duration_seconds

        conn.execute(
            """
            UPDATE items
            SET status = 'transcribed', transcript_path = ?, metadata = ?, updated_at = ?
            WHERE id = ?
            """,
            (transcript_rel, json.dumps(metadata), now_iso, item["id"]),
        )
        conn.commit()

        logger.info("Transcribed item %s (%s)", item["id"], video_id)
        return FetchResult(success=True)

    @staticmethod
    def _mark_error(conn: sqlite3.Connection, item: Item, error_msg: str) -> None:
        """Mark an item as errored: increment retry_count, store error in metadata."""
        now_iso = datetime.now(UTC).isoformat()
        try:
            metadata: dict[str, object] = json.loads(str(item.get("metadata", "{}")))
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
            (json.dumps(metadata), now_iso, item["id"]),
        )
        conn.commit()
