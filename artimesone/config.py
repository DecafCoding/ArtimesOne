"""Application configuration loaded from environment variables.

Implements the hybrid prefix rule from plan §11.1: variables that a third-party SDK
reads natively (``OPENAI_API_KEY``, ``OPENAI_BASE_URL``, ``APIFY_TOKEN``) keep their
bare names; everything ArtimesOne invented gets the ``ARTIMESONE_`` prefix.

Every field has a safe default so that ``Settings()`` constructs successfully with
zero environment variables set — this is the graceful-degradation contract from
plan §11.3 and PRD §10.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ArtimesOne runtime configuration.

    All fields are optional. Field values are resolved in this order:
    explicit kwargs → environment variables → ``.env`` file → defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARTIMESONE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ---
    data_dir: Path = Field(default=Path("./data"))
    content_dir: Path = Field(default=Path("./content"))
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)

    # --- LLM (OpenAI / OpenAI-compatible) ---
    # Bare-name vars: validation_alias bypasses the class-level ARTIMESONE_ prefix.
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY"),
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL"),
    )
    summary_model: str = Field(default="openai:gpt-4o-mini")
    chat_model: str = Field(default="openai:gpt-4o")

    # --- YouTube ---
    youtube_api_key: str | None = Field(default=None)
    max_video_duration_minutes: int = Field(default=60)

    # --- Apify (Phase 2) ---
    apify_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIFY_TOKEN"),
    )
    apify_youtube_actor: str = Field(default="streamers/youtube-scraper")

    # --- Telegram (Phase 5) ---
    # Stored as a string because Telegram chat IDs can be negative 64-bit integers
    # and we treat them as opaque identifiers.
    telegram_bot_token: str | None = Field(default=None)
    telegram_allowed_chat_id: str | None = Field(default=None)
