"""The data tools bundled as one in-process SDK MCP server.

Every tool reads only, except ``propose_calendar_events``, which stores a
calendar proposal for the user to confirm. No tool creates calendar events:
code does that after the user's "네" (``event_proposals``).
"""

from __future__ import annotations

from typing import Iterable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from .calendar_tool import get_schedule
from .credits_tool import get_credits
from .dropbox_tool import check_dropbox_updates, make_check_dropbox_updates
from .propose_tool import make_propose_calendar_events, propose_calendar_events
from .weather_tool import get_weather

SERVER_NAME = "mungchi"
SERVER_VERSION = "0.1.0"

ALL_TOOLS = [check_dropbox_updates, get_schedule, get_weather, get_credits, propose_calendar_events]


def mcp_tool_name(tool_name: str) -> str:
    """Fully qualified name Claude sees: ``mcp__<server>__<tool>``."""
    return f"mcp__{SERVER_NAME}__{tool_name}"


DROPBOX_TOOL = mcp_tool_name(check_dropbox_updates.name)
CALENDAR_TOOL = mcp_tool_name(get_schedule.name)
WEATHER_TOOL = mcp_tool_name(get_weather.name)
CREDITS_TOOL = mcp_tool_name(get_credits.name)
PROPOSE_TOOL = mcp_tool_name(propose_calendar_events.name)

# 업뎃 (``update``) and 일정 (``schedule``) as 고뭉치's subagents. 업뎃 stays
# Dropbox-only under 고뭉치; notes to put in the calendar go to 일정.
UPDATE_TOOLS = [DROPBOX_TOOL]
SCHEDULE_TOOLS = [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
# 업뎃 and 일정 answering the user directly (their own Slack bot, ``--agent``):
# both can turn a pasted note into a calendar proposal.
UPDATE_DIRECT_TOOLS = [DROPBOX_TOOL, PROPOSE_TOOL]
SCHEDULE_DIRECT_TOOLS = list(SCHEDULE_TOOLS)
# 고뭉치's main agent calls these two read-only, cheap tools itself; Dropbox,
# the calendar and proposals stay delegated to 업뎃 and 일정. Nobody else gets get_credits.
MUNGCHI_TOOLS = [CREDITS_TOOL, WEATHER_TOOL]
DATA_TOOLS = [DROPBOX_TOOL, CALENDAR_TOOL, WEATHER_TOOL, CREDITS_TOOL, PROPOSE_TOOL]


def data_tools(*, briefing: bool = False, conversation_key: str | None = None) -> list[SdkMcpTool]:
    """Every data tool for one run, the run-bound ones built for that run.

    ``briefing=True`` only for briefing runs (see ``make_check_dropbox_updates``).
    ``conversation_key`` is where this run's calendar proposal is kept (see
    ``make_propose_calendar_events``). Fresh tool objects per call keep both
    bound to one run.
    """
    return [
        make_check_dropbox_updates(briefing=briefing),
        get_schedule,
        get_weather,
        get_credits,
        make_propose_calendar_events(conversation_key),
    ]


def tools_named(
    names: Iterable[str], *, briefing: bool = False, conversation_key: str | None = None
) -> list[SdkMcpTool]:
    """The SDK tool objects (built for one run, see ``data_tools``) whose fully qualified names are in ``names``."""
    wanted = set(names)
    return [
        t
        for t in data_tools(briefing=briefing, conversation_key=conversation_key)
        if mcp_tool_name(t.name) in wanted
    ]


def build_server(tools: Iterable[SdkMcpTool] | None = None) -> McpSdkServerConfig:
    """The ``mungchi`` server with ``tools`` (default: every data tool, ad-hoc mode, no conversation)."""
    selected = data_tools() if tools is None else list(tools)
    return create_sdk_mcp_server(name=SERVER_NAME, version=SERVER_VERSION, tools=selected)


__all__ = [
    "ALL_TOOLS",
    "CALENDAR_TOOL",
    "CREDITS_TOOL",
    "DATA_TOOLS",
    "DROPBOX_TOOL",
    "MUNGCHI_TOOLS",
    "PROPOSE_TOOL",
    "SCHEDULE_DIRECT_TOOLS",
    "SCHEDULE_TOOLS",
    "SERVER_NAME",
    "UPDATE_DIRECT_TOOLS",
    "UPDATE_TOOLS",
    "WEATHER_TOOL",
    "build_server",
    "data_tools",
    "mcp_tool_name",
    "tools_named",
]
