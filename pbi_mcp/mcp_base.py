"""MCPServer that lets a tool's deliberate ValueError reach the model.

Newer `mcp` releases (2.1+) treat any exception other than `ToolError` as a crash and tell the model only
"Error executing tool <name>". This project reports expected, fixable problems (a missing argument, "Editing
is off: set PBI_MCP_EDIT_ROOT", "Wrong approval code (2 tries left)") as `ValueError`, and the model needs
that text to recover. Other exceptions stay generic so internals are not leaked. On older `mcp` releases,
which already pass the message through, this class behaves exactly like `MCPServer`.
"""
from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer

try:
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
except ImportError:  # older mcp: ValueError messages already reach the client
    ToolError = UnexpectedToolError = None  # type: ignore[assignment,misc]


class PbiMCPServer(MCPServer):
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        name = str(args[0] if args else kwargs.get("name", "?"))
        started = time.monotonic()
        try:
            result = await super().call_tool(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged unless it is a deliberate ValueError
            cause = exc.__cause__ if (UnexpectedToolError is not None and isinstance(exc, UnexpectedToolError)) else exc
            _learn(name, False, started, str(cause) if isinstance(cause, (ValueError, ToolError or ValueError)) else type(cause).__name__)
            if UnexpectedToolError is not None and isinstance(exc, UnexpectedToolError) and isinstance(exc.__cause__, ValueError):
                raise ToolError(_with_hint(name, str(exc.__cause__))) from exc.__cause__
            raise
        _learn(name, not (getattr(result, "is_error", False) or getattr(result, "isError", False)), started, None)
        return result


def _learn(name: str, ok: bool, started: float, error: str | None) -> None:
    """Record the call in the local learning log (tool name, timing, redacted error only). Never raises."""
    try:
        from .plus import learning
        learning.observe(name, ok, (time.monotonic() - started) * 1000, error)
    except Exception:  # noqa: BLE001
        pass


def _with_hint(name: str, message: str) -> str:
    try:
        from .plus import learning
        hint = learning.hint_for(name, message)
        return f"{message}\n{hint}" if hint else message
    except Exception:  # noqa: BLE001
        return message
