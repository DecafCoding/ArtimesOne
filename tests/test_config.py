"""Tests for artimesone.config — env-var loading and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from artimesone.config import Settings


def test_zero_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() succeeds with no env vars set."""
    # Clear anything that might leak from the user's environment.
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "APIFY_TOKEN",
        "ARTIMESONE_YOUTUBE_API_KEY",
        "ARTIMESONE_DATA_DIR",
        "ARTIMESONE_CONTENT_DIR",
        "ARTIMESONE_HOST",
        "ARTIMESONE_PORT",
        "ARTIMESONE_TELEGRAM_BOT_TOKEN",
        "ARTIMESONE_TELEGRAM_ALLOWED_CHAT_ID",
        "ARTIMESONE_SUMMARY_MODEL",
        "ARTIMESONE_CHAT_MODEL",
        "ARTIMESONE_MAX_VIDEO_DURATION_MINUTES",
        "ARTIMESONE_APIFY_YOUTUBE_ACTOR",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.data_dir == Path("./data")
    assert s.content_dir == Path("./content")
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.openai_api_key is None
    assert s.youtube_api_key is None


def test_openai_bare_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENAI_API_KEY (no ARTIMESONE_ prefix) is picked up."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.openai_api_key == "sk-test"


def test_artimesone_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARTIMESONE_YOUTUBE_API_KEY is picked up via the class-level prefix."""
    monkeypatch.setenv("ARTIMESONE_YOUTUBE_API_KEY", "yt-test")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.youtube_api_key == "yt-test"
