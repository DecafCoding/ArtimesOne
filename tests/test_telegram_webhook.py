"""Tests for the Telegram webhook route and the split_message helper.

The webhook tests use a real ``telegram.Bot`` instance (never initialized,
so no network I/O) with its ``send_message`` / ``edit_message_text`` methods
replaced by ``AsyncMock``. The background-task entry point ``_handle_message``
is patched to an ``AsyncMock`` in non-``/start`` tests so the chat agent is
never actually invoked — this keeps the tests fast, deterministic, and offline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from telegram import Bot

from artimesone.telegram.stream import split_message

# A format-valid Telegram token. ``telegram.Bot.__init__`` validates the
# ``<digits>:<chars>`` shape but does not hit the network unless ``initialize()``
# is called — we never call it.
_FAKE_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
_ALLOWED_CHAT_ID = 12345


def _make_update(
    chat_id: int = _ALLOWED_CHAT_ID,
    text: str | None = "hello",
) -> dict[str, Any]:
    """Build a minimal Telegram Update JSON payload."""
    message: dict[str, Any] = {
        "message_id": 1,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": 1, "message": message}


def _make_mock_bot() -> Bot:
    """Build a Bot instance with its network-touching methods mocked out.

    ``telegram.Bot`` inherits ``TelegramObject``'s frozen ``__setattr__``, so
    we call ``_unfreeze()`` (a protected PTB helper meant for exactly this
    use case) before replacing the async methods with ``AsyncMock``s.
    """
    bot = Bot(token=_FAKE_TOKEN)
    bot._unfreeze()
    bot.send_message = AsyncMock()  # type: ignore[method-assign]
    bot.edit_message_text = AsyncMock()  # type: ignore[method-assign]
    return bot


# ---------------------------------------------------------------------------
# split_message — pure function tests
# ---------------------------------------------------------------------------


def test_split_message_empty_returns_empty_list() -> None:
    assert split_message("") == []


def test_split_message_short_returns_single_chunk() -> None:
    assert split_message("hello") == ["hello"]


def test_split_message_exact_max_len_returns_single_chunk() -> None:
    text = "a" * 100
    assert split_message(text, max_len=100) == [text]


def test_split_message_splits_at_paragraph_boundary() -> None:
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"
    chunks = split_message(text, max_len=20)
    assert all(len(c) <= 20 for c in chunks)
    assert chunks == ["first paragraph", "second paragraph", "third paragraph"]


def test_split_message_splits_at_single_newline_when_no_paragraph_break() -> None:
    text = "line one here\nline two here\nline three here"
    chunks = split_message(text, max_len=15)
    assert all(len(c) <= 15 for c in chunks)
    # No chunk starts with whitespace — continuation newlines are stripped.
    for c in chunks:
        assert not c.startswith("\n")


def test_split_message_splits_at_space_when_no_newlines() -> None:
    text = " ".join(["word"] * 50)
    chunks = split_message(text, max_len=20)
    assert all(len(c) <= 20 for c in chunks)
    # Continuation chunks never start with a space — the splitter strips them.
    for c in chunks:
        assert not c.startswith(" ")


def test_split_message_hard_break_when_no_boundary() -> None:
    text = "x" * 100
    chunks = split_message(text, max_len=10)
    assert len(chunks) == 10
    assert all(c == "x" * 10 for c in chunks)
    assert "".join(chunks) == text


def test_split_message_continuation_strips_leading_whitespace() -> None:
    text = "a" * 18 + "\n\n" + "b" * 18
    chunks = split_message(text, max_len=20)
    assert chunks == ["a" * 18, "b" * 18]


# ---------------------------------------------------------------------------
# Webhook route — graceful-degradation (no bot configured)
# ---------------------------------------------------------------------------


async def test_webhook_returns_404_when_token_missing(
    client: httpx.AsyncClient,
) -> None:
    """With no bot token configured, the route is silently disabled (404)."""
    r = await client.post("/telegram/webhook", json=_make_update())
    assert r.status_code == 404


async def test_app_boots_with_zero_env_vars_telegram_disabled(
    app: FastAPI,
) -> None:
    """The existing graceful-degradation contract holds: app.state.telegram_bot is None."""
    assert getattr(app.state, "telegram_bot", "missing") is None


# ---------------------------------------------------------------------------
# Webhook route — with mock bot
# ---------------------------------------------------------------------------


async def test_webhook_dispatches_valid_update_to_background_task(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """A valid update from the allowed chat schedules _handle_message."""
    app.state.telegram_bot = _make_mock_bot()
    app.state.settings.telegram_allowed_chat_id = str(_ALLOWED_CHAT_ID)

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post(
            "/telegram/webhook",
            json=_make_update(chat_id=_ALLOWED_CHAT_ID, text="hello"),
        )

    assert r.status_code == 200
    handle_mock.assert_awaited_once()
    # The first positional arg is the bot; third is the user text.
    args = handle_mock.await_args.args
    assert args[1] == _ALLOWED_CHAT_ID
    assert args[2] == "hello"


async def test_webhook_rejects_unauthorized_chat_silently(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Messages from any chat_id other than the allowed one are dropped silently."""
    bot = _make_mock_bot()
    app.state.telegram_bot = bot
    app.state.settings.telegram_allowed_chat_id = str(_ALLOWED_CHAT_ID)

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post(
            "/telegram/webhook",
            json=_make_update(chat_id=99999, text="hello"),
        )

    assert r.status_code == 200
    handle_mock.assert_not_called()
    bot.send_message.assert_not_called()  # type: ignore[attr-defined]


async def test_webhook_rejects_when_allowed_chat_id_unset(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """If telegram_allowed_chat_id is None, every message is rejected silently."""
    app.state.telegram_bot = _make_mock_bot()
    app.state.settings.telegram_allowed_chat_id = None

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post("/telegram/webhook", json=_make_update())

    assert r.status_code == 200
    handle_mock.assert_not_called()


async def test_webhook_start_command_sends_welcome(client: httpx.AsyncClient, app: FastAPI) -> None:
    """/start returns the welcome message and never dispatches to the agent."""
    bot = _make_mock_bot()
    app.state.telegram_bot = bot
    app.state.settings.telegram_allowed_chat_id = str(_ALLOWED_CHAT_ID)

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post(
            "/telegram/webhook",
            json=_make_update(chat_id=_ALLOWED_CHAT_ID, text="/start"),
        )

    assert r.status_code == 200
    bot.send_message.assert_awaited_once()  # type: ignore[attr-defined]
    call = bot.send_message.await_args  # type: ignore[attr-defined]
    assert call.kwargs["chat_id"] == _ALLOWED_CHAT_ID
    assert "ArtimesOne" in call.kwargs["text"]
    handle_mock.assert_not_called()


async def test_webhook_ignores_update_with_missing_text(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """An update with no text field is silently ignored."""
    app.state.telegram_bot = _make_mock_bot()
    app.state.settings.telegram_allowed_chat_id = str(_ALLOWED_CHAT_ID)

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post(
            "/telegram/webhook",
            json=_make_update(chat_id=_ALLOWED_CHAT_ID, text=None),
        )

    assert r.status_code == 200
    handle_mock.assert_not_called()


async def test_webhook_ignores_update_without_message(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """An update payload with no 'message' field (e.g. callback_query) is ignored."""
    app.state.telegram_bot = _make_mock_bot()
    app.state.settings.telegram_allowed_chat_id = str(_ALLOWED_CHAT_ID)

    handle_mock = AsyncMock()
    with patch("artimesone.telegram.webhook._handle_message", handle_mock):
        r = await client.post("/telegram/webhook", json={"update_id": 1})

    assert r.status_code == 200
    handle_mock.assert_not_called()


async def test_webhook_ignores_non_json_body(client: httpx.AsyncClient, app: FastAPI) -> None:
    """A malformed (non-JSON) request body is acknowledged with 200 and ignored."""
    app.state.telegram_bot = _make_mock_bot()

    r = await client.post(
        "/telegram/webhook",
        content=b"not json at all",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
