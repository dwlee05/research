"""Read-only data tools bundled as one in-process SDK MCP server."""

from __future__ import annotations

from typing import Iterable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from .calendar_tool import get_schedule
from .dropbox_tool import check_dropbox_updates, make_check_dropbox_updates

SERVER_NAME = "mungchi"
SERVER_VERSION = "0.1.0"

ALL_TOOLS = [check_dropbox_updates, get_schedule]


def mcp_tool_name(tool_name: str) -> str:
    """Fully qualified name Claude sees: ``mcp__<server>__<tool>``."""
    return f"mcp__{SERVER_NAME}__{tool_name}"


DROPBOX_TOOL = mcp_tool_name(check_dropbox_updates.name)
CALENDAR_TOOL = mcp_tool_name(get_schedule.name)

# 업뎃 (``update``) and 일정 (``schedule``) each own their tools.
UPDATE_TOOLS = [DROPBOX_TOOL]
SCHEDULE_TOOLS = [CALENDAR_TOOL]
DATA_TOOLS = UPDATE_TOOLS + SCHEDULE_TOOLS


def data_tools(*, briefing: bool = False) -> list[SdkMcpTool]:
    """Every data tool for one run, the Dropbox tool built for that run's mode.

    ``briefing=True`` only for briefing runs (see ``make_check_dropbox_updates``).
    A fresh Dropbox tool object per call keeps the mode bound to one run.
    """
    return [make_check_dropbox_updates(briefing=briefing), get_schedule]


def tools_named(names: Iterable[str], *, briefing: bool = False) -> list[SdkMcpTool]:
    """The SDK tool objects (built for one run, see ``data_tools``) whose fully qualified names are in ``names``."""
    wanted = set(names)
    return [t for t in data_tools(briefing=briefing) if mcp_tool_name(t.name) in wanted]


def build_server(tools: Iterable[SdkMcpTool] | None = None) -> McpSdkServerConfig:
    """The ``mungchi`` server with ``tools`` (default: every data tool, ad-hoc mode)."""
    selected = data_tools() if tools is None else list(tools)
    return create_sdk_mcp_server(name=SERVER_NAME, version=SERVER_VERSION, tools=selected)


__all__ = [
    "ALL_TOOLS",
    "CALENDAR_TOOL",
    "DATA_TOOLS",
    "DROPBOX_TOOL",
    "SCHEDULE_TOOLS",
    "SERVER_NAME",
    "UPDATE_TOOLS",
    "build_server",
    "data_tools",
    "mcp_tool_name",
    "tools_named",
]
