"""Throttled edit-in-place streaming helper and message splitter.

Manages the Telegram streaming lifecycle: send a placeholder, accumulate
tokens from the chat agent, periodically edit the message with new content,
and do a final formatted HTML edit.  Long messages are split at paragraph
boundaries to stay within Telegram's 4096-char limit.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from pydantic_ai.messages import ToolCallPart

from artimesone.telegram.format import markdown_to_telegram_html, strip_to_plain

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from telegram import Bot

    from artimesone.agents.chat import ChatDeps
    from artimesone.config import Settings

logger = logging.getLogger(__name__)

# Minimum interval between edit_message_text calls (seconds).
_EDIT_INTERVAL = 0.75

# Tools whose invocations produce parenthetical notes in the response.
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


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split *text* into chunks that each fit within *max_len* characters.

    Splitting preference order: double-newline (paragraph boundary), single
    newline, space, hard break.  Uses ``max_len=4000`` by default as a safety
    margin below Telegram's 4096-char limit.
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        cut = text[:max_len]

        # Prefer paragraph boundary.
        pos = cut.rfind("\n\n")
        if pos <= 0:
            pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len

        chunks.append(text[:pos])
        text = text[pos:].lstrip()

    return chunks


def _tool_description(tool_name: str, args: dict[str, object] | str | None) -> str:
    """Build a short description of a write-tool invocation for parentheticals."""
    parsed: dict[str, object]
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
        case "create_rollup":
            title = parsed.get("title", "")
            return f'Created rollup: "{title}"'
        case "update_rollup":
            return f"Updated rollup {parsed.get('rollup_id', '')}"
        case "add_tag_to_item":
            tag = parsed.get("tag", "")
            return f'Added topic "{tag}" to item {parsed.get("item_id", "")}'
        case "add_source":
            name = parsed.get("name", "")
            return f'Following new source: "{name}"'
        case "enable_source":
            return f"Enabled source {parsed.get('source_id', '')}"
        case "disable_source":
            return f"Disabled source {parsed.get('source_id', '')}"
        case _:
            return f"Ran {tool_name}"


async def stream_response(
    bot: Bot,
    chat_id: int,
    agent: Agent[ChatDeps, str],
    user_message: str,
    deps: ChatDeps,
    settings: Settings,  # noqa: ARG001
) -> None:
    """Run the chat agent with streaming and relay tokens to Telegram.

    Lifecycle:
    1. Send a placeholder ``"…"`` message.
    2. Stream tokens from ``agent.run_stream()``, editing the message every
       ~750 ms with accumulated plain text.
    3. Collect write-tool invocations for parenthetical notes.
    4. After streaming, convert to HTML and do a final formatted edit.
       Fall back to plain text if Telegram rejects the HTML.
    5. Split into multiple messages if the result exceeds 4000 chars.
    """
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, TimedOut

    # 1. Send placeholder.
    sent = await bot.send_message(chat_id=chat_id, text="\u2026")
    message_id = sent.message_id

    buffer = ""
    last_edit = time.monotonic()
    write_tool_notes: list[str] = []

    try:
        async with agent.run_stream(user_message, deps=deps) as stream:
            # 3. Collect tool-call info before streaming text.
            for msg in stream.all_messages():
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolCallPart) and part.tool_name in WRITE_TOOLS:
                            desc = _tool_description(part.tool_name, part.args)
                            write_tool_notes.append(desc)

            # 2. Stream text tokens with throttled edits.
            async for chunk in stream.stream_text(delta=True):
                buffer += chunk
                now = time.monotonic()
                if (now - last_edit) >= _EDIT_INTERVAL and buffer.strip():
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=buffer,
                        )
                        last_edit = now
                    except BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            logger.warning("Telegram edit failed: %s", exc)
                    except TimedOut:
                        logger.debug("Telegram edit timed out, will retry on next tick")

    except Exception:
        logger.exception("Chat agent error during Telegram streaming")
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Sorry, something went wrong while generating a response.",
            )
        except Exception:
            logger.exception("Failed to send error message to Telegram")
        return

    if not buffer.strip():
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="No response generated.",
            )
        except Exception:
            logger.exception("Failed to send empty-response message to Telegram")
        return

    # 6. Append write-tool parentheticals.
    if write_tool_notes:
        notes = "\n".join(f"_({note})_" for note in write_tool_notes)
        buffer = buffer.rstrip() + "\n\n" + notes

    # 7. Convert to HTML and do the final edit.
    html = markdown_to_telegram_html(buffer)
    chunks = split_message(html)

    # First chunk replaces the existing message.
    first_chunk = chunks[0] if chunks else html
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=first_chunk,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        # HTML rejected — fall back to plain text.
        logger.debug("HTML final edit rejected, falling back to plain text")
        plain = strip_to_plain(buffer)
        plain_chunks = split_message(plain)
        first_plain = plain_chunks[0] if plain_chunks else plain
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=first_plain,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Plain text final edit also failed: %s", exc)
        # Send overflow plain chunks as new messages.
        for extra in plain_chunks[1:]:
            try:
                await bot.send_message(chat_id=chat_id, text=extra)
            except Exception:
                logger.exception("Failed to send overflow plain chunk")
        return

    # Send overflow HTML chunks as new messages.
    for extra in chunks[1:]:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=extra,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            # Fall back to plain text for this chunk.
            plain_extra = strip_to_plain(extra)
            try:
                await bot.send_message(chat_id=chat_id, text=plain_extra)
            except Exception:
                logger.exception("Failed to send overflow chunk")
        except Exception:
            logger.exception("Failed to send overflow HTML chunk")
