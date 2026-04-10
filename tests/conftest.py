"""Shared test fixtures.

All fixtures use tmp_path for data/content directories so tests are isolated
and cleaned up automatically by pytest.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from artimesone.app import create_app
from artimesone.config import Settings
from artimesone.db import get_connection
from artimesone.migrations import apply_migrations


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings that point at temp directories."""
    return Settings(data_dir=tmp_path / "data", content_dir=tmp_path / "content")


@pytest.fixture()
def conn(tmp_settings: Settings) -> Iterator[sqlite3.Connection]:
    """Migrated SQLite connection for unit tests."""
    db_path = tmp_settings.data_dir / "artimesone.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = get_connection(db_path)
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
async def app(tmp_settings: Settings) -> AsyncIterator[FastAPI]:
    """FastAPI app with lifespan run against temp directories."""
    with patch("artimesone.app.Settings", return_value=tmp_settings):
        application = create_app()
        async with LifespanManager(application):
            yield application


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the test app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
