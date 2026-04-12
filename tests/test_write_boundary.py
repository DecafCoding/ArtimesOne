"""Write-boundary integrity test (PRD §13 Risk #3).

Asserts programmatically that the chat agent's tool surface:
- contains exactly the 15 expected tools from PRD §6.1-6.3,
- does not write to raw tables (``items``, ``collection_runs``) or raw
  markdown paths (``content/transcripts/``, ``content/summaries/``).

SQLite has no row-level access control; this test is the only automated
gate enforcing the application-layer write boundary documented in
PRD §8 (raw/derived matrix).
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from pydantic_ai.models.test import TestModel

from artimesone.agents import tools as tools_module
from artimesone.agents.chat import create_chat_agent

EXPECTED_READ_TOOLS = {
    "search_items",
    "get_item",
    "get_transcript",
    "list_recent_items",
    "list_topics",
    "list_sources",
    "get_stats",
    "list_rollups",
    "get_rollup",
}
EXPECTED_WRITE_TOOLS = {
    "create_rollup",
    "update_rollup",
    "add_tag_to_item",
}
EXPECTED_SOURCE_TOOLS = {
    "add_source",
    "enable_source",
    "disable_source",
}
EXPECTED_ALL = EXPECTED_READ_TOOLS | EXPECTED_WRITE_TOOLS | EXPECTED_SOURCE_TOOLS

FORBIDDEN_PATTERNS = [
    re.compile(r"INSERT\s+INTO\s+items\b", re.IGNORECASE),
    re.compile(r"UPDATE\s+items\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+items\b", re.IGNORECASE),
    re.compile(r"INSERT\s+INTO\s+collection_runs\b", re.IGNORECASE),
    re.compile(r"UPDATE\s+collection_runs\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+collection_runs\b", re.IGNORECASE),
    re.compile(r"transcripts/"),
    re.compile(r"summaries/"),
]


def _get_registered_tool_names(agent: Any) -> set[str]:
    """Extract the set of registered tool names from an Agent instance.

    pydantic-ai 1.x exposes the function toolset at ``agent._function_toolset``
    with a dict-like ``.tools`` attribute keyed by tool name.
    """
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None and hasattr(toolset, "tools"):
        return set(toolset.tools.keys())
    raise AssertionError(
        "Could not introspect agent tool set — update "
        "_get_registered_tool_names for the installed pydantic-ai version"
    )


def test_registered_tool_set_matches_prd() -> None:
    """The chat agent registers exactly the 15 tools from PRD §6.1-6.3."""
    agent = create_chat_agent(model=TestModel())
    registered = _get_registered_tool_names(agent)
    assert registered == EXPECTED_ALL, (
        f"Tool surface drift — expected {sorted(EXPECTED_ALL)}, got {sorted(registered)}"
    )


def test_no_tool_writes_to_raw_tables() -> None:
    """Static scan of each tool function's source code finds no forbidden writes."""
    for tool_name in sorted(EXPECTED_ALL):
        func = getattr(tools_module, tool_name, None)
        assert func is not None, f"Tool {tool_name} not found in tools module"
        source = inspect.getsource(func)
        for pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(source)
            assert match is None, (
                f"Tool {tool_name} contains forbidden raw-table/path write: "
                f"{match.group() if match else 'unknown'}"
            )
