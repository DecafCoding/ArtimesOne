"""FastAPI application factory and dependency helpers.

Wires the startup sequence: load config → ensure directories → run migrations →
build and start the scheduler → mount routes and templates. The lifespan context
manager owns the scheduler lifecycle.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import get_connection
from .migrations import apply_migrations
from .scheduler import build_scheduler, reload_jobs
from .web.filters import register_filters


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown sequence per plan §2.5."""
    settings = Settings()

    # Ensure runtime directories exist.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for subdir in ("transcripts", "summaries", "rollups"):
        (settings.content_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Run migrations.
    db_path = settings.data_dir / "artimesone.db"
    conn = get_connection(db_path)
    try:
        applied = apply_migrations(conn)
        if applied:
            logging.getLogger(__name__).info("Applied migrations: %s", applied)
    finally:
        conn.close()

    # Build and start the scheduler.
    scheduler = build_scheduler(settings)
    reload_jobs(scheduler, settings)
    scheduler.start()

    # Initialize the Telegram bot if configured (plan §8).
    telegram_bot = None
    if settings.telegram_bot_token:
        from telegram import Bot

        telegram_bot = Bot(token=settings.telegram_bot_token)
        await telegram_bot.initialize()
        logging.getLogger(__name__).info("Telegram bot initialized")

    # Stash on app.state for dependency injection.
    app.state.settings = settings
    app.state.scheduler = scheduler
    app.state.db_path = db_path
    app.state.telegram_bot = telegram_bot

    yield

    if app.state.telegram_bot is not None:
        await app.state.telegram_bot.shutdown()
    scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    """Construct the FastAPI application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(title="ArtimesOne", lifespan=lifespan)

    # Static files.
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Templates.
    templates_dir = Path(__file__).parent / "web" / "templates"
    app.state.templates = Jinja2Templates(directory=templates_dir)
    register_filters(app.state.templates.env)

    # Routers.
    from .telegram.webhook import router as telegram_router
    from .web.routes.chat import router as chat_router
    from .web.routes.dashboard import router as dashboard_router
    from .web.routes.items import router as items_router
    from .web.routes.rollups import router as rollups_router
    from .web.routes.runs import router as runs_router
    from .web.routes.sources import router as sources_router
    from .web.routes.topics import router as topics_router

    app.include_router(chat_router)
    app.include_router(dashboard_router)
    app.include_router(items_router)
    app.include_router(rollups_router)
    app.include_router(runs_router)
    app.include_router(sources_router)
    app.include_router(topics_router)
    app.include_router(telegram_router)

    return app


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def get_settings(request: Request) -> Settings:
    """Retrieve the shared Settings instance from app state."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived SQLite connection, closed after the request."""
    conn = get_connection(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()
