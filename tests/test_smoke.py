"""End-to-end v1 smoke test — the canonical 'is v1 still working?' signal.

Walks the full PRD §10 MVP Success Definition in one test:

    1. seed a source via POST /sources
    2. scheduled collection runs (YouTube + Apify stubbed)
    3. item appears on the dashboard
    4. all browse surfaces render (200)
    5. chat query triggers tool calls and creates a rollup
    6. the rollup page renders

All external services are stubbed:

- YouTube Data API + Apify REST API → respx
- Summarizer agent → pydantic-ai ``FunctionModel`` via ``agent.override(...)``
- Chat agent → real factory (so tool registrations are exercised) but with
  its model replaced by a scripted ``FunctionModel``.

Target runtime is under 2 seconds — enforced by a ``time.monotonic`` check
at the end of the test so a regression surfaces loudly.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import respx
from fastapi import FastAPI
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)

from artimesone.agents.chat import create_chat_agent as real_create_chat_agent
from artimesone.agents.summarizer import create_summarizer_agent
from artimesone.db import get_connection
from artimesone.scheduler import run_source_collection

# ---------------------------------------------------------------------------
# Stubs — shamelessly borrowed from tests/test_scheduler_pipeline.py
# ---------------------------------------------------------------------------

_YT_BASE = "https://www.googleapis.com/youtube/v3"
_APIFY_SYNC_URL = (
    "https://api.apify.com/v2/acts/streamers~youtube-scraper/run-sync-get-dataset-items"
)

_SUMMARY_JSON = json.dumps(
    {
        "summary": "This video covers LoRA fine-tuning on consumer GPUs.",
        "topics": ["lora", "fine-tuning", "large-language-models"],
    }
)

_SAMPLE_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:05,000\n"
    "Welcome to the video\n"
    "\n"
    "2\n"
    "00:00:06,000 --> 00:00:10,000\n"
    "Today we discuss LoRA fine-tuning\n"
)

_SAMPLE_APIFY_ITEM = {
    "subtitles": [
        {
            "srt": _SAMPLE_SRT,
            "type": "auto_generated",
            "language": "en",
        }
    ],
    "description": "A great video about LoRA.",
    "duration": 600,
}


async def _summarizer_handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Return a canned summary JSON regardless of input."""
    return ModelResponse(parts=[TextPart(content=_SUMMARY_JSON)])


_ROLLUP_ARGS_JSON = json.dumps(
    {
        "title": "LoRA Roundup",
        "body": "A quick summary of LoRA fine-tuning coverage.",
        "topics": ["lora"],
        "source_item_ids": [],
    }
)


async def _chat_stream(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls]:
    """Stateful streaming chat handler: search_items → create_rollup → text.

    Dispatches on the number of ``ModelResponse`` messages already in the
    history: turn 0 → search, turn 1 → create rollup, turn 2 → reply text.
    The chat route calls ``agent.run_stream`` so FunctionModel needs a
    stream function.
    """
    prior_responses = sum(1 for m in messages if isinstance(m, ModelResponse))
    if prior_responses == 0:
        yield {
            0: DeltaToolCall(
                name="search_items",
                json_args='{"query": "lora"}',
                tool_call_id="call_search",
            )
        }
        return
    if prior_responses == 1:
        yield {
            0: DeltaToolCall(
                name="create_rollup",
                json_args=_ROLLUP_ARGS_JSON,
                tool_call_id="call_rollup",
            )
        }
        return
    yield "Here's what I found: LoRA fine-tuning videos."


def _mock_youtube_api(video_ids: list[str]) -> None:
    respx.get(f"{_YT_BASE}/channels").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]},
        )
    )
    playlist_items = [{"contentDetails": {"videoId": vid}, "snippet": {}} for vid in video_ids]
    respx.get(f"{_YT_BASE}/playlistItems").mock(
        return_value=httpx.Response(200, json={"items": playlist_items})
    )
    detail_items = [
        {
            "id": vid,
            "snippet": {"title": f"LoRA Video {vid}", "publishedAt": "2026-01-01T00:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
        }
        for vid in video_ids
    ]
    respx.get(f"{_YT_BASE}/videos").mock(
        return_value=httpx.Response(200, json={"items": detail_items})
    )


def _mock_apify_success() -> None:
    respx.post(_APIFY_SYNC_URL).mock(return_value=httpx.Response(200, json=[_SAMPLE_APIFY_ITEM]))


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@respx.mock
async def test_v1_end_to_end(client: httpx.AsyncClient, app: FastAPI, tmp_path: Path) -> None:
    """Full v1 user journey in one test, <2s wall clock."""
    started = time.monotonic()

    # --- Arrange: enable external-service-gated phases on app settings. ---
    app.state.settings.youtube_api_key = "fake-yt-key"
    app.state.settings.apify_token = "fake-apify-token"
    app.state.settings.openai_api_key = "sk-fake-openai-key"

    # --- Stub YouTube + Apify for discovery and transcript fetch. ---
    _mock_youtube_api(["smoke_vid1"])
    _mock_apify_success()

    # --- Act 1: seed a source via the real POST /sources route. ---
    seed_response = await client.post(
        "/sources",
        data={
            "type": "youtube_channel",
            "external_id": "UCtest",
            "name": "Smoke Test Channel",
        },
        follow_redirects=False,
    )
    assert seed_response.status_code == 303, seed_response.text

    # Look up the source id out-of-band.
    db_conn = get_connection(app.state.db_path)
    try:
        source_row = db_conn.execute(
            "SELECT id FROM sources WHERE external_id = 'UCtest'"
        ).fetchone()
        assert source_row is not None
        source_id: int = source_row["id"]
    finally:
        db_conn.close()

    # --- Act 2: run the full pipeline with summarizer stubbed. ---
    summarizer = create_summarizer_agent(model="test")

    def _patched_create_summarizer(model: str = "test") -> Agent[None, Any]:
        return summarizer

    with (
        summarizer.override(model=FunctionModel(_summarizer_handler)),
        patch(
            "artimesone.pipeline.summarize.create_summarizer_agent",
            _patched_create_summarizer,
        ),
    ):
        await run_source_collection(source_id, app.state.settings)

    # Verify the pipeline produced a summarized item before touching the UI.
    db_conn = get_connection(app.state.db_path)
    try:
        item_row = db_conn.execute(
            "SELECT id, title, status FROM items WHERE external_id = 'smoke_vid1'"
        ).fetchone()
        assert item_row is not None
        assert item_row["status"] == "summarized"
    finally:
        db_conn.close()

    # --- Act 3: every browse surface renders. ---
    for path in ("/", "/items", "/topics", "/rollups", "/runs", "/chat"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"

    # Dashboard should mention the seeded item title.
    dashboard = await client.get("/")
    assert "LoRA Video smoke_vid1" in dashboard.text

    # --- Act 4: chat agent runs tool calls and creates a rollup. ---
    def _patched_create_chat_agent(model: str = "test") -> Agent[Any, str]:
        # Call the real factory so register_tools() runs; swap the model.
        return real_create_chat_agent(model=FunctionModel(stream_function=_chat_stream))

    with patch("artimesone.web.routes.chat.create_chat_agent", _patched_create_chat_agent):
        chat_response = await client.post("/chat/send", data={"message": "any videos on lora?"})
    assert chat_response.status_code == 200
    assert "text/event-stream" in chat_response.headers.get("content-type", "")

    # --- Assert: a rollup row now exists, and its detail page renders. ---
    db_conn = get_connection(app.state.db_path)
    try:
        rollup_row = db_conn.execute(
            "SELECT id, title FROM rollups ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert rollup_row is not None, "Chat agent did not create a rollup"
        assert rollup_row["title"] == "LoRA Roundup"
        rollup_id: int = rollup_row["id"]
    finally:
        db_conn.close()

    rollup_page = await client.get(f"/rollups/{rollup_id}")
    assert rollup_page.status_code == 200
    assert "LoRA Roundup" in rollup_page.text

    # --- Budget guard: fail loudly if the smoke test balloons past 2s. ---
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"Smoke test took {elapsed:.2f}s (target <2s)"
