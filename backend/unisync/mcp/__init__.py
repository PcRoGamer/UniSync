"""UniSync Model Context Protocol (MCP) Server.

Provides AI agents (Hermes, Cursor, Claude Desktop, Antigravity) with programmatic
read-write access to university deadlines, assignments, course materials, lecture
transcripts, and announcements.
"""

from typing import Any

__all__ = ["mcp"]


def __getattr__(name: str) -> Any:
    if name == "mcp":
        from unisync.mcp.server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

