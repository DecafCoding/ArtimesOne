"""Tests for /sources CRUD routes."""

from __future__ import annotations

import httpx


async def test_add_source(client: httpx.AsyncClient) -> None:
    """POST /sources creates a source and redirects to listing."""
    r = await client.post(
        "/sources",
        data={"type": "youtube_channel", "external_id": "UCtest", "name": "Test Channel"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Test Channel" in r.text


async def test_duplicate_source_rejected(client: httpx.AsyncClient) -> None:
    """Adding the same external_id twice shows an error message."""
    await client.post(
        "/sources",
        data={"type": "youtube_channel", "external_id": "UCdup", "name": "First"},
        follow_redirects=True,
    )
    r = await client.post(
        "/sources",
        data={"type": "youtube_channel", "external_id": "UCdup", "name": "Second"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "already exists" in r.text


async def test_enable_disable_delete(client: httpx.AsyncClient) -> None:
    """Full cycle: add → disable → enable → delete."""
    # Add
    await client.post(
        "/sources",
        data={"type": "youtube_channel", "external_id": "UCcycle", "name": "Cycle Test"},
        follow_redirects=True,
    )

    # List and find the source ID from the disable form action
    r = await client.get("/sources")
    assert "Cycle Test" in r.text

    # Extract source ID from the HTML (e.g., action="/sources/1/disable")
    import re

    match = re.search(r"/sources/(\d+)/disable", r.text)
    assert match is not None
    source_id = match.group(1)

    # Disable → the row now renders an Enable button (form action ends in /enable)
    r = await client.post(f"/sources/{source_id}/disable", follow_redirects=True)
    assert r.status_code == 200
    assert f"/sources/{source_id}/enable" in r.text

    # Enable → the row renders a Disable button again
    r = await client.post(f"/sources/{source_id}/enable", follow_redirects=True)
    assert r.status_code == 200
    assert f"/sources/{source_id}/disable" in r.text

    # Delete
    r = await client.post(f"/sources/{source_id}/delete", follow_redirects=True)
    assert r.status_code == 200
    assert "Cycle Test" not in r.text
