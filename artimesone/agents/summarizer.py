"""Summarizer agent — turns transcripts into decision-support summaries.

Uses pydantic-ai's Agent with output_type=VideoSummary to produce typed
structured output: 1–2 paragraphs of prose plus 3–7 topic tags. The agent
is non-streaming, has no tools, and is called by the pipeline (not the user).
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings


class VideoSummary(BaseModel):
    """Structured output from the summarizer agent.

    summary: 1–2 paragraphs of prose, leading with the subject matter.
    topics: 3–7 tags, lowercase, hyphenated, specific.
    """

    summary: str
    topics: list[str]


SUMMARIZER_SYSTEM_PROMPT = """\
You summarize video transcripts for a personal knowledge index. Your summary \
is a decision-support filter: it helps the reader decide whether the video is \
worth their time.

Write 1–2 short paragraphs of prose. Lead with the subject matter — get directly \
to what the video is actually about. Do not write meta-phrasing like "In this \
video..." or "The speaker discusses...". The reader already knows it's a video.

Also produce 3–7 topic tags. Tags are lowercase, hyphenated, and specific. Prefer \
"retrieval-augmented-generation" over "ai", "quantization" over "optimization", \
"apache-iceberg" over "data"."""


def create_summarizer_agent(model: str = "openai:gpt-4o-mini") -> Agent[None, VideoSummary]:
    """Create the summarizer agent with the given model string.

    The agent is constructed lazily (not at app startup) so the app boots
    without an OpenAI key. The pipeline calls this when it needs to summarize.
    """
    return Agent(
        model,
        output_type=VideoSummary,
        system_prompt=SUMMARIZER_SYSTEM_PROMPT,
        model_settings=ModelSettings(temperature=0.2, max_tokens=500),
    )
