"""MCPServer that lets a tool's deliberate ValueError reach the model.

Newer `mcp` releases (2.1+) treat any exception other than `ToolError` as a crash and tell the model only
"Error executing tool <name>". This project reports expected, fixable problems (a missing argument, "Editing
is off: set PBI_MCP_EDIT_ROOT", "Wrong approval code (2 tries left)") as `ValueError`, and the model needs
that text to recover. Other exceptions stay generic so internals are not leaked. On older `mcp` releases,
which already pass the message through, this class behaves exactly like `MCPServer`.
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

try:
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
except ImportError:  # older mcp: ValueError messages already reach the client
    ToolError = UnexpectedToolError = None  # type: ignore[assignment,misc]


class PbiMCPServer(MCPServer):
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().call_tool(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged unless it is a deliberate ValueError
            if UnexpectedToolError is not None and isinstance(exc, UnexpectedToolError) and isinstance(exc.__cause__, ValueError):
                raise ToolError(str(exc.__cause__)) from exc.__cause__
            raise
