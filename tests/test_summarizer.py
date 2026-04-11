"""Tests for artimesone.agents.summarizer — offline via TestModel / FunctionModel."""

from __future__ import annotations

import json

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from artimesone.agents.summarizer import VideoSummary, create_summarizer_agent


def test_create_summarizer_agent_returns_agent() -> None:
    """Factory returns an Agent instance (using test model to avoid needing an API key)."""
    agent = create_summarizer_agent(model="test")
    assert isinstance(agent, Agent)


async def test_summarizer_returns_video_summary() -> None:
    """TestModel produces a valid VideoSummary with the right shape."""
    agent = create_summarizer_agent(model="test")
    result = await agent.run("This is a sample transcript about machine learning.")
    assert isinstance(result.output, VideoSummary)
    assert isinstance(result.output.summary, str)
    assert len(result.output.summary) > 0
    assert isinstance(result.output.topics, list)


async def test_summarizer_with_function_model() -> None:
    """FunctionModel lets us control exact output for assertions."""

    async def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                TextPart(
                    content=json.dumps(
                        {
                            "summary": "LoRA fine-tuning lets you adapt large language "
                            "models on consumer GPUs by training low-rank matrices "
                            "instead of full weight updates.",
                            "topics": ["lora", "fine-tuning", "large-language-models"],
                        }
                    )
                )
            ]
        )

    agent = create_summarizer_agent(model="test")
    with agent.override(model=FunctionModel(handler)):
        result = await agent.run("Transcript about LoRA fine-tuning techniques.")
    assert result.output.summary.startswith("LoRA fine-tuning")
    assert "lora" in result.output.topics
    assert len(result.output.topics) == 3


def test_video_summary_model_valid() -> None:
    """VideoSummary accepts valid data."""
    vs = VideoSummary(summary="A summary.", topics=["topic-a", "topic-b"])
    assert vs.summary == "A summary."
    assert vs.topics == ["topic-a", "topic-b"]


def test_video_summary_model_empty_topics() -> None:
    """VideoSummary accepts an empty topics list (no min constraint in v1)."""
    vs = VideoSummary(summary="A summary.", topics=[])
    assert vs.topics == []
