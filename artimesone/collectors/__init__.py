"""Collector framework — the Protocol every source-type adapter implements.

A *collector* is the only component allowed to write to the raw region of the
database (``sources``, ``items``, ``collection_runs``, ``content/transcripts/``,
``content/summaries/``). The agent and the web UI never call collectors
directly — the scheduler does.

Two-phase model (plan §10):
    discover()  Cheap metadata sweep — list new items from the source, insert
                rows into ``items`` with ``status='discovered'``. Phase 1 of
                ArtimesOne implements this for YouTube.
    fetch()     Expensive per-item fetch — pull the transcript / body and write
                a md file under ``content/``. Lands in Phase 2.

Signature note (deviation from plan §10.1)
------------------------------------------
The plan sketches::

    async def discover(self, source: Source) -> DiscoverResult: ...

For Phase 1 the collector needs DB access (to insert ``items`` rows and check
the stop-at-known set) and config access (for ``max_video_duration_minutes``
and the YouTube API key), so the Phase-1 signature passes ``conn`` and
``settings`` explicitly. This is cleaner than stashing them on the collector
instance and matches the dependency-injection pattern used elsewhere in the
project. Documented as a deliberate deviation in phase1-foundation.md §NOTES.

Registry (plan §10.7)
---------------------
``COLLECTORS`` is a plain ``dict[str, Collector]``. Phase 1 adds a single
entry — ``"youtube_channel"`` — in Task 7. The dict is populated lazily by
``_register_defaults()`` so that importing this module never triggers a
collector import that might pull in optional deps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol, TypedDict

if TYPE_CHECKING:
    from artimesone.config import Settings


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverResult:
    """Outcome of a single ``discover()`` call.

    ``discovered`` and ``filtered_out`` are mutually exclusive counters of items
    examined this run; ``error`` is set when discovery hit a planned failure
    (missing API key, channel not found, etc.) — unplanned exceptions
    propagate to the scheduler instead.
    """

    discovered: int
    filtered_out: int
    error: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a single ``fetch()`` call (Phase 2)."""

    success: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


class Source(TypedDict):
    """The shape of a row from the ``sources`` table.

    ``config`` is a JSON-encoded string — collectors parse it to extract
    source-type-specific settings (channel ID, poll cron, etc.).
    """

    id: int
    type: str
    external_id: str
    name: str
    config: str
    enabled: int
    created_at: str
    updated_at: str


class Item(TypedDict):
    """The shape of a row from the ``items`` table."""

    id: int
    source_id: int
    external_id: str
    title: str
    url: str | None
    published_at: str | None
    fetched_at: str
    metadata: str
    status: str
    transcript_path: str | None
    summary_path: str | None
    retry_count: int
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Collector(Protocol):
    """The contract every source-type adapter must satisfy."""

    source_type: ClassVar[str]

    async def discover(
        self,
        source: Source,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> DiscoverResult: ...

    async def fetch(
        self,
        item: Item,
        conn: sqlite3.Connection,
        settings: Settings,
    ) -> FetchResult: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


COLLECTORS: dict[str, Collector] = {}


def _register_defaults() -> None:
    """Populate ``COLLECTORS`` with the built-in collectors.

    Imports happen lazily inside this function so a circular import in any
    one collector module doesn't break the package import.
    """
    from .youtube.collector import YouTubeChannelCollector

    COLLECTORS["youtube_channel"] = YouTubeChannelCollector()


_register_defaults()
