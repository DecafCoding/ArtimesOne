"""Chat route — SSE streaming endpoint and chat history persistence.

Implements the ``/chat`` route per plan section 7.5: HTMX form submit opens an SSE
connection, tokens stream into the response div, tool calls surface inline.
Chat history is persisted to ``chat_messages`` per section 7.8.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...agents.chat import ChatDeps, create_chat_agent
from ...app import get_db, get_settings
from ...config import Settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Tools classified by tier for UI visibility (plan section 7.5).
WRITE_TOOLS = frozenset(
    {
        "create_rollup",
        "update_rollup",
        "add_tag_to_item",
        "add_source",
        "enable_source",
        "disable_source",
    }
)

# ---------------------------------------------------------------------------
# Chat history helpers (plan section 7.8)
# ---------------------------------------------------------------------------


def save_message(
    conn: sqlite3.Connection,
    role: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
) -> int:
    """Insert a message into ``chat_messages``. Returns the new row id."""
    now_iso = datetime.now(UTC).isoformat()
    tool_calls_json = json.dumps(tool_calls) if tool_calls else None
    cursor = conn.execute(
        "INSERT INTO chat_messages (role, content, tool_calls, created_at) VALUES (?, ?, ?, ?)",
        (role, content, tool_calls_json, now_iso),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def load_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Load the most recent *limit* chat messages, oldest first."""
    rows = conn.execute(
        """
        SELECT id, role, content, tool_calls, created_at
        FROM chat_messages
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    messages: list[dict[str, Any]] = []
    for row in reversed(rows):
        tc = row["tool_calls"]
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "tool_calls": json.loads(tc) if tc else None,
                "created_at": row["created_at"],
            }
        )
    return messages


def clear_history(conn: sqlite3.Connection) -> None:
    """Delete all rows from ``chat_messages``."""
    conn.execute("DELETE FROM chat_messages")
    conn.commit()


# ---------------------------------------------------------------------------
# Tool-call description helpers
# ---------------------------------------------------------------------------


def _tool_description(tool_name: str, args: dict[str, Any] | str | None) -> str:
    """Build a short human-readable description of a tool invocation."""
    parsed: dict[str, Any]
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    elif args is None:
        parsed = {}
    else:
        parsed = args

    match tool_name:
        case "search_items":
            query = parsed.get("query", "")
            return f'Searching items for "{query}"...'
        case "get_item":
            return f"Reading item {parsed.get('item_id', '')}..."
        case "get_transcript":
            return f"Reading transcript for item {parsed.get('item_id', '')}..."
        case "list_recent_items":
            return "Listing recent items..."
        case "list_topics":
            return "Listing topics..."
        case "list_sources":
            return "Listing sources..."
        case "get_stats":
            return "Getting corpus stats..."
        case "list_rollups":
            return "Listing rollups..."
        case "get_rollup":
            return f"Reading rollup {parsed.get('rollup_id', '')}..."
        case "create_rollup":
            title = parsed.get("title", "")
            return f'Created rollup: "{title}"'
        case "update_rollup":
            return f"Updated rollup {parsed.get('rollup_id', '')}."
        case "add_tag_to_item":
            tag = parsed.get("tag", "")
            return f'Added topic "{tag}" to item {parsed.get("item_id", "")}.'
        case "add_source":
            name = parsed.get("name", "")
            return f'Following new source: "{name}"'
        case "enable_source":
            return f"Enabled source {parsed.get('source_id', '')}."
        case "disable_source":
            return f"Disabled source {parsed.get('source_id', '')}."
        case _:
            return f"Calling {tool_name}..."


def _tool_is_write(tool_name: str) -> bool:
    """Return True if the tool is a write or source-management tool."""
    return tool_name in WRITE_TOOLS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Render the chat page with history."""
    messages = load_history(conn)
    has_api_key = settings.openai_api_key is not None
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "chat.html",
        {
            "messages": messages,
            "has_api_key": has_api_key,
        },
    )


@router.post("/chat/send")
async def chat_send(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Accept a user message, run the chat agent with streaming, return SSE events.

    The response is ``text/event-stream``. Event types:
    - ``tool``: a tool call indicator (data is JSON with ``name``, ``description``, ``is_write``)
    - ``token``: a text chunk from the assistant (data is the raw text)
    - ``done``: signals end of response (data is empty)
    """
    from starlette.responses import StreamingResponse

    form = await request.form()
    user_message = str(form.get("message", "")).strip()

    if not user_message:
        return HTMLResponse("")

    if settings.openai_api_key is None:
        save_message(conn, "user", user_message)
        error_content = "I can't respond right now — no LLM API key is configured."
        save_message(conn, "assistant", error_content)
        return HTMLResponse(
            f'<div class="chat-msg chat-msg-assistant">{error_content}</div>',
        )

    # Persist user message.
    save_message(conn, "user", user_message)

    async def event_stream() -> AsyncIterator[str]:
        """Generate SSE events from the agent's streaming response."""
        tool_calls_log: list[dict[str, Any]] = []
        full_text = ""

        try:
            agent = create_chat_agent(model=settings.chat_model)
            deps = ChatDeps(
                conn=conn,
                settings=settings,
                scheduler=getattr(request.app.state, "scheduler", None),
            )

            # Load previous messages for context (pydantic-ai message_history).
            # We pass the chat history as the user prompt only — pydantic-ai
            # handles the system prompt internally.
            async with agent.run_stream(user_message, deps=deps) as stream:
                # pydantic-ai runs tool-call rounds internally before
                # yielding the StreamedRunResult.  By the time we enter
                # this block, all tool calls have completed and are
                # recorded in all_messages().  Emit indicators first,
                # then stream text tokens.
                from pydantic_ai.messages import ToolCallPart

                for msg in stream.all_messages():
                    if hasattr(msg, "parts"):
                        for part in msg.parts:
                            if isinstance(part, ToolCallPart):
                                tool_name: str = part.tool_name
                                args = part.args
                                desc = _tool_description(tool_name, args)
                                is_write = _tool_is_write(tool_name)
                                tool_data = json.dumps(
                                    {
                                        "name": tool_name,
                                        "description": desc,
                                        "is_write": is_write,
                                    }
                                )
                                tool_calls_log.append(
                                    {
                                        "name": tool_name,
                                        "description": desc,
                                        "is_write": is_write,
                                    }
                                )
                                yield f"event: tool\ndata: {tool_data}\n\n"

                # Stream text tokens.
                async for chunk in stream.stream_text(delta=True):
                    full_text += chunk
                    escaped = chunk.replace("\n", "\\n")
                    yield f"event: token\ndata: {escaped}\n\n"

            # Persist assistant response.
            save_message(
                conn,
                "assistant",
                full_text,
                tool_calls_log if tool_calls_log else None,
            )

        except Exception:
            logger.exception("Chat agent error")
            error_msg = "Sorry, something went wrong while generating a response."
            yield f"event: token\ndata: {error_msg}\n\n"
            save_message(conn, "assistant", error_msg)

        yield "event: done\ndata: \n\n"

    return StreamingResponse(  # type: ignore[return-value]
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/clear", response_class=HTMLResponse)
async def chat_clear(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> HTMLResponse:
    """Clear all chat history and redirect to the chat page."""
    clear_history(conn)
    from starlette.responses import RedirectResponse

    return RedirectResponse(url="/chat", status_code=303)  # type: ignore[return-value]
