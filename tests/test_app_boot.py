"""Tests for app boot — verifies the app starts and serves pages."""

from __future__ import annotations

import httpx


async def test_app_boots_with_zero_env_vars(client: httpx.AsyncClient) -> None:
    """The dashboard responds 200 even with no API keys configured."""
    r = await client.get("/")
    assert r.status_code == 200
    assert "ArtimesOne" in r.text


async def test_sources_page_is_empty_initially(client: httpx.AsyncClient) -> None:
    """The sources page shows 'No sources yet' on a fresh DB."""
    r = await client.get("/sources")
    assert r.status_code == 200
    assert "No sources yet" in r.text
