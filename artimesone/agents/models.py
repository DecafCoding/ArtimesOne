"""Pydantic return models for chat agent tools.

These are the typed shapes that read tools return to the LLM. Kept in a
separate module to avoid circular imports between ``chat.py`` and
``tools.py``.  All models are flat and serialization-friendly — no
``sqlite3.Row``, ``Path``, or other opaque types.
"""

from __future__ import annotations

from pydantic import BaseModel


class ItemSummary(BaseModel):
    """Compact item representation for search results and listings."""

    id: int
    title: str
    url: str | None
    published_at: str | None
    source_name: str
    summary_snippet: str | None
    topics: list[str]


class ItemDetail(BaseModel):
    """Full item representation including summary prose and metadata."""

    id: int
    title: str
    url: str | None
    published_at: str | None
    source_name: str
    summary: str | None
    topics: list[str]
    status: str
    duration_seconds: int | float | None
    thumbnail_url: str | None


class TopicInfo(BaseModel):
    """Topic tag with its item count."""

    slug: str
    name: str
    item_count: int


class SourceInfo(BaseModel):
    """Source registration record."""

    id: int
    type: str
    external_id: str
    name: str
    enabled: bool


class CorpusStats(BaseModel):
    """Aggregate statistics across the whole corpus."""

    total_items: int
    total_sources: int
    total_topics: int
    items_by_status: dict[str, int]
    last_collection_run: str | None


class RollupSummary(BaseModel):
    """Compact rollup for list views."""

    id: int
    title: str
    topics: list[str]
    generated_by: str
    created_at: str


class RollupDetail(BaseModel):
    """Full rollup with body text and cited source items."""

    id: int
    title: str
    topics: list[str]
    generated_by: str
    created_at: str
    body: str
    source_items: list[ItemSummary]
