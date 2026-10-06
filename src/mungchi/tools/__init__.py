"""Read-only data tools bundled as one in-process SDK MCP server."""

from __future__ import annotations

from typing import Iterable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from .calendar_tool import get_schedule
from .dropbox_tool import check_dropbox_updates
from .overleaf_tool import check_overleaf_updates

SERVER_NAME = "mungchi"
SERVER_VERSION = "0.1.0"

ALL_TOOLS = [check_dropbox_updates, check_overleaf_updates, get_schedule]


def mcp_tool_name(tool_name: str) -> str:
    """Fully qualified name Claude sees: ``mcp__<server>__<tool>``."""
    return f"mcp__{SERVER_NAME}__{tool_name}"


DROPBOX_TOOL = mcp_tool_name(check_dropbox_updates.name)
OVERLEAF_TOOL = mcp_tool_name(check_overleaf_updates.name)
CALENDAR_TOOL = mcp_tool_name(get_schedule.name)

# 업뎃 (``update``) and 일정 (``schedule``) each own their tools.
UPDATE_TOOLS = [DROPBOX_TOOL, OVERLEAF_TOOL]
SCHEDULE_TOOLS = [CALENDAR_TOOL]
DATA_TOOLS = UPDATE_TOOLS + SCHEDULE_TOOLS


def tools_named(names: Iterable[str]) -> list[SdkMcpTool]:
    """The SDK tool objects whose fully qualified names are in ``names``."""
    wanted = set(names)
    return [t for t in ALL_TOOLS if mcp_tool_name(t.name) in wanted]


def build_server(tools: Iterable[SdkMcpTool] | None = None) -> McpSdkServerConfig:
    """The ``mungchi`` server with ``tools`` (default: all three data tools)."""
    selected = ALL_TOOLS if tools is None else list(tools)
    return create_sdk_mcp_server(name=SERVER_NAME, version=SERVER_VERSION, tools=selected)


__all__ = [
    "ALL_TOOLS",
    "CALENDAR_TOOL",
    "DATA_TOOLS",
    "DROPBOX_TOOL",
    "OVERLEAF_TOOL",
    "SCHEDULE_TOOLS",
    "SERVER_NAME",
    "UPDATE_TOOLS",
    "build_server",
    "mcp_tool_name",
    "tools_named",
]
