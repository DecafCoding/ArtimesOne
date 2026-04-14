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

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Mirror .env into os.environ so bare-name third-party SDK vars (OPENAI_API_KEY,
# APIFY_TOKEN) reach the SDKs that read os.environ directly. pydantic-settings
# reads .env into Settings fields but does not populate os.environ, so without
# this the openai/apify clients can't see keys that live only in .env.
# override=False preserves the "shell env wins over .env" precedence.
load_dotenv(override=False)


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
    # Cold-start cap: maximum number of videos discovered the first time a
    # channel is visited (before any items exist for that source).
    initial_video_cap: int = Field(default=20)
    # Rolling cap: maximum number of new videos discovered per round after
    # the first visit. If a channel posts more than this in a single day,
    # the overflow rolls into the next round.
    rolling_video_cap: int = Field(default=3)

    # --- Scheduler ---
    # Single round job cron. Each round processes up to 5 sources whose
    # last_check_at is NULL or more than 24h old.
    round_cron: str = Field(default="*/30 * * * *")

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
