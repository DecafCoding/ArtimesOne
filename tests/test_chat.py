"""Tests for the chat route — SSE streaming, history persistence, and clear."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from artimesone.db import get_connection
from artimesone.web.routes.chat import (
    WRITE_TOOLS,
    _tool_description,
    _tool_is_write,
    clear_history,
    load_history,
    save_message,
)

# ---------------------------------------------------------------------------
# Unit tests: persistence helpers
# ---------------------------------------------------------------------------


def test_save_and_load_messages(conn: sqlite3.Connection) -> None:
    """Messages round-trip through save_message / load_history."""
    save_message(conn, "user", "Hello!")
    save_message(conn, "assistant", "Hi there!", [{"name": "search_items"}])

    history = load_history(conn)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello!"
    assert history[0]["tool_calls"] is None
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hi there!"
    assert history[1]["tool_calls"] == [{"name": "search_items"}]


def test_load_history_returns_oldest_first(conn: sqlite3.Connection) -> None:
    """load_history returns messages in chronological order."""
    save_message(conn, "user", "First")
    save_message(conn, "user", "Second")
    save_message(conn, "user", "Third")

    history = load_history(conn)
    assert [m["content"] for m in history] == ["First", "Second", "Third"]


def test_load_history_respects_limit(conn: sqlite3.Connection) -> None:
    """load_history only returns the most recent N messages."""
    for i in range(10):
        save_message(conn, "user", f"Message {i}")

    history = load_history(conn, limit=3)
    assert len(history) == 3
    assert history[0]["content"] == "Message 7"
    assert history[2]["content"] == "Message 9"


def test_clear_history(conn: sqlite3.Connection) -> None:
    """clear_history removes all messages."""
    save_message(conn, "user", "Hello")
    save_message(conn, "assistant", "Hi")
    assert len(load_history(conn)) == 2

    clear_history(conn)
    assert len(load_history(conn)) == 0


# ---------------------------------------------------------------------------
# Unit tests: tool description helpers
# ---------------------------------------------------------------------------


def test_tool_description_search_items() -> None:
    assert 'Searching items for "RAG"' in _tool_description("search_items", {"query": "RAG"})


def test_tool_description_create_rollup() -> None:
    desc = _tool_description("create_rollup", {"title": "Weekly digest"})
    assert "Weekly digest" in desc


def test_tool_description_unknown_tool() -> None:
    desc = _tool_description("unknown_tool", {})
    assert "unknown_tool" in desc


def test_tool_description_string_args() -> None:
    """String args are parsed as JSON."""
    desc = _tool_description("search_items", '{"query": "test"}')
    assert "test" in desc


def test_tool_is_write() -> None:
    assert _tool_is_write("create_rollup") is True
    assert _tool_is_write("add_source") is True
    assert _tool_is_write("search_items") is False
    assert _tool_is_write("get_item") is False


def test_write_tools_set_covers_all_write_tools() -> None:
    """WRITE_TOOLS contains all write and source-management tools from plan section 6."""
    expected = {
        "create_rollup",
        "update_rollup",
        "add_tag_to_item",
        "add_source",
        "enable_source",
        "disable_source",
    }
    assert WRITE_TOOLS == expected


# ---------------------------------------------------------------------------
# Integration tests: chat page
# ---------------------------------------------------------------------------


async def test_chat_page_renders(client: httpx.AsyncClient) -> None:
    """GET /chat returns 200."""
    r = await client.get("/chat")
    assert r.status_code == 200
    assert "Chat" in r.text


async def test_chat_page_shows_no_api_key_message(client: httpx.AsyncClient) -> None:
    """When no OPENAI_API_KEY is set, the chat page shows a configure message."""
    r = await client.get("/chat")
    assert r.status_code == 200
    assert "OPENAI_API_KEY" in r.text


async def test_chat_nav_link_present(client: httpx.AsyncClient) -> None:
    """The nav bar includes a Chat link."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "/chat" in r.text


async def test_chat_send_no_api_key_persists_messages(client: httpx.AsyncClient, app: Any) -> None:
    """POST /chat/send without API key persists user msg and returns error."""
    r = await client.post("/chat/send", data={"message": "Hello agent"})
    assert r.status_code == 200
    assert "no LLM API key" in r.text.lower() or "api key" in r.text.lower()

    # Verify messages were persisted.
    db_conn = get_connection(app.state.db_path)
    try:
        history = load_history(db_conn)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello agent"
        assert history[1]["role"] == "assistant"
    finally:
        db_conn.close()


async def test_chat_send_empty_message(client: httpx.AsyncClient) -> None:
    """POST /chat/send with empty message returns empty response."""
    r = await client.post("/chat/send", data={"message": ""})
    assert r.status_code == 200
    assert r.text == ""


async def test_chat_clear(client: httpx.AsyncClient, app: Any) -> None:
    """POST /chat/clear removes all messages and redirects."""
    # Seed some messages.
    db_conn = get_connection(app.state.db_path)
    try:
        save_message(db_conn, "user", "Hello")
        save_message(db_conn, "assistant", "Hi")
        assert len(load_history(db_conn)) == 2
    finally:
        db_conn.close()

    r = await client.post("/chat/clear", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"

    # Verify messages were cleared.
    db_conn = get_connection(app.state.db_path)
    try:
        assert len(load_history(db_conn)) == 0
    finally:
        db_conn.close()


async def test_chat_history_renders_on_page_load(client: httpx.AsyncClient, app: Any) -> None:
    """Chat history is rendered server-side on page load."""
    db_conn = get_connection(app.state.db_path)
    try:
        save_message(db_conn, "user", "What about RAG?")
        save_message(db_conn, "assistant", "RAG is a retrieval technique.")
    finally:
        db_conn.close()

    r = await client.get("/chat")
    assert r.status_code == 200
    assert "What about RAG?" in r.text
    assert "RAG is a retrieval technique." in r.text


async def test_chat_send_with_api_key_streams_sse(client: httpx.AsyncClient, app: Any) -> None:
    """POST /chat/send with API key returns SSE content-type and events."""
    from unittest.mock import patch

    from pydantic_ai import Agent

    # Patch settings to have an API key.
    app.state.settings.openai_api_key = "sk-test-fake"

    # Override the chat agent to use TestModel so no real API call is made.
    original_create = __import__(
        "artimesone.agents.chat", fromlist=["create_chat_agent"]
    ).create_chat_agent

    def mock_create_chat_agent(model: str = "test") -> Agent[Any, str]:
        return original_create(model="test")

    with patch("artimesone.web.routes.chat.create_chat_agent", mock_create_chat_agent):
        r = await client.post("/chat/send", data={"message": "Hello"})

    assert r.status_code == 200
    content_type = r.headers.get("content-type", "")
    assert "text/event-stream" in content_type

    # The response should contain SSE events.
    body = r.text
    assert "event: token" in body or "event: done" in body

    # Verify messages were persisted.
    db_conn = get_connection(app.state.db_path)
    try:
        history = load_history(db_conn)
        # Should have user + assistant messages.
        roles = [m["role"] for m in history]
        assert "user" in roles
        assert "assistant" in roles
    finally:
        db_conn.close()
