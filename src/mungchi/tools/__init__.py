"""Read-only data tools bundled as one in-process SDK MCP server."""

from __future__ import annotations

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server

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

UPDEOT_TOOLS = [DROPBOX_TOOL, OVERLEAF_TOOL]
PPALIT_TOOLS = [CALENDAR_TOOL]
DATA_TOOLS = UPDEOT_TOOLS + PPALIT_TOOLS


def build_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(name=SERVER_NAME, version=SERVER_VERSION, tools=ALL_TOOLS)


__all__ = [
    "ALL_TOOLS",
    "CALENDAR_TOOL",
    "DATA_TOOLS",
    "DROPBOX_TOOL",
    "OVERLEAF_TOOL",
    "PPALIT_TOOLS",
    "SERVER_NAME",
    "UPDEOT_TOOLS",
    "build_server",
    "mcp_tool_name",
]
