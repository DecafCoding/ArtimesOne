"""Telegram webhook route.

Implements plan section 8.2: a single FastAPI endpoint that receives
Telegram bot updates, enforces the single-user guard, and dispatches
user messages to the chat agent via a background task.

Feature is silently disabled (404) when ``ARTIMESONE_TELEGRAM_BOT_TOKEN``
is unset — matching the graceful-degradation contract from plan §11.3.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from ..agents.chat import ChatDeps, create_chat_agent
from ..app import get_settings
from ..config import Settings
from ..db import get_connection
from .stream import stream_response

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from telegram import Bot

logger = logging.getLogger(__name__)
router = APIRouter()

WELCOME_MESSAGE = "Hi, I'm ArtimesOne. Ask me anything about your corpus."
ERROR_MESSAGE = "Sorry, something went wrong while processing your message."


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    background_tasks: BackgroundTasks,
) -> Response:
    """Receive a Telegram update and dispatch it to the chat agent.

    Returns:
    - 404 when the bot token is not configured (feature disabled)
    - 200 for all other valid webhook calls, including silently-rejected
      messages from unauthorized chat IDs. Telegram retries webhooks that
      don't return 200 within ~5s, so we always acknowledge quickly and
      process the message as a background task.
    """
    bot: Bot | None = getattr(request.app.state, "telegram_bot", None)
    if bot is None:
        return Response(status_code=404)

    try:
        data: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("Telegram webhook received non-JSON body")
        return Response(status_code=200)

    # Import locally so the module doesn't hard-require python-telegram-bot
    # at import time when the feature is disabled.
    from telegram import Update

    update = Update.de_json(data, bot)
    if update is None:
        return Response(status_code=200)

    message = update.message or update.edited_message
    if message is None or not message.text:
        return Response(status_code=200)

    chat_id = message.chat_id
    text = message.text

    # Single-user guard — silently reject messages from anyone else (§8.2).
    allowed = settings.telegram_allowed_chat_id
    if allowed is None or str(chat_id) != allowed:
        logger.info("Rejected Telegram message from chat_id=%s", chat_id)
        return Response(status_code=200)

    if text == "/start":
        try:
            await bot.send_message(chat_id=chat_id, text=WELCOME_MESSAGE)
        except Exception:
            logger.exception("Failed to send /start welcome message")
        return Response(status_code=200)

    scheduler: AsyncIOScheduler | None = getattr(request.app.state, "scheduler", None)

    background_tasks.add_task(
        _handle_message,
        bot,
        chat_id,
        text,
        settings,
        scheduler,
    )
    return Response(status_code=200)


async def _handle_message(
    bot: Bot,
    chat_id: int,
    text: str,
    settings: Settings,
    scheduler: AsyncIOScheduler | None,
) -> None:
    """Run the chat agent and stream the response back to Telegram.

    Opens its own database connection because the request-scoped
    connection from ``Depends(get_db)`` is closed by the time a
    FastAPI background task runs.
    """
    db_path = settings.data_dir / "artimesone.db"
    conn = get_connection(db_path)
    try:
        deps = ChatDeps(
            conn=conn,
            settings=settings,
            scheduler=scheduler,
            is_telegram=True,
        )
        agent = create_chat_agent(model=settings.chat_model)
        await stream_response(bot, chat_id, agent, text, deps, settings)
    except Exception:
        logger.exception("Telegram background handler failed")
        try:
            await bot.send_message(chat_id=chat_id, text=ERROR_MESSAGE)
        except Exception:
            logger.exception("Failed to send Telegram error message")
    finally:
        conn.close()
