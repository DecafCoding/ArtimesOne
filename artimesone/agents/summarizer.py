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

Write 1–2 short paragraphs of prose. The first ~200 characters are shown as a \
preview, so every word in the opening must carry information about the subject.

**The first sentence must start with the subject matter, not with a reference \
to the video.** Do not begin with "This video...", "The video...", "In this \
video...", "This tutorial...", "The speaker...", "The presenter...", or any \
variant that describes the video itself before describing what it is about. \
The reader already knows it's a video — wasting the preview on "This video \
showcases / explores / features / covers / demonstrates..." is the single \
biggest failure mode of this task.

Bad: "This video showcases a walkthrough of fine-tuning Llama 3 on a single GPU."
Bad: "The video explores how QLoRA reduces VRAM requirements."
Good: "Fine-tuning Llama 3 on a single 24GB GPU with QLoRA, including rank \
settings and VRAM trade-offs."
Good: "QLoRA cuts VRAM requirements for Llama 3 fine-tuning by quantizing the \
base weights to 4-bit while training LoRA adapters in higher precision."

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
