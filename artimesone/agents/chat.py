"""Chat agent — conversational interface over the collected corpus.

Constructs a pydantic-ai ``Agent[ChatDeps, str]`` with the system prompt
from plan section 6.4 and all 15 tools from sections 6.1-6.3.  The agent is built
lazily via ``create_chat_agent()`` so the app boots without an API key.

Tools are registered inside the factory by calling ``register_tools()``
from ``agents/tools.py``, avoiding circular imports.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai import Agent, RunContext

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from artimesone.config import Settings

CHAT_SYSTEM_PROMPT = """\
You are the user's personal assistant. You have access to a corpus of collected \
content — articles, video transcripts, and summaries gathered from sources the \
user subscribes to.

Use your tools to look up information before answering. Do not guess or fabricate \
content. If a tool returns no results, say so rather than inventing an answer.

Cite sources by title and link when referencing specific items.

If the user asks about something not in the corpus, say so.

When the user asks for synthesis across multiple items, create a rollup with \
`create_rollup` so the work persists. Before synthesizing fresh, check \
`list_rollups` for an existing rollup that already answers the question.

Create a new rollup rather than updating one that is older than a day, or one \
that addresses a different question. Only call `update_rollup` when extending \
an in-progress synthesis from the same conversation. This produces a natural \
daily history of rollups on recurring topics.

If you notice an item is missing a topic that clearly applies, you may add it \
with `add_tag_to_item`. Only add tags that genuinely apply — err on the side \
of fewer, more accurate tags.

If the user asks you to follow a new source, use `add_source` (auto-enabled) \
and confirm what was added. If the user asks to stop a source, use \
`disable_source` rather than asking them to do it manually."""

TELEGRAM_SYSTEM_ADDENDUM = (
    "Your response is being sent via Telegram and read on a phone. "
    "Prefer shorter replies. Lead with the answer; skip preamble. "
    "Use inline links instead of long quoted blocks."
)


@dataclass
class ChatDeps:
    """Dependencies injected into every tool call via ``RunContext[ChatDeps]``."""

    conn: sqlite3.Connection
    settings: Settings
    scheduler: AsyncIOScheduler | None = None
    is_telegram: bool = False


def create_chat_agent(model: str = "openai:gpt-4o") -> Agent[ChatDeps, str]:
    """Create the chat agent with all tools registered.

    Constructed lazily (not at import time) so the app boots without an
    API key.  The model string follows pydantic-ai's provider format.
    """
    agent: Agent[ChatDeps, str] = Agent(
        model,
        deps_type=ChatDeps,
        output_type=str,
        system_prompt=CHAT_SYSTEM_PROMPT,
    )

    # Register tools on the agent instance.  Importing here avoids a
    # circular dependency (tools.py needs ChatDeps, chat.py needs tools).
    from artimesone.agents.tools import register_tools

    register_tools(agent)

    @agent.system_prompt
    def _telegram_addendum(ctx: RunContext[ChatDeps]) -> str:
        if ctx.deps.is_telegram:
            return TELEGRAM_SYSTEM_ADDENDUM
        return ""

    return agent
