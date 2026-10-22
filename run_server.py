"""Entry point for launching the MCP server from an external host (e.g.
Claude Desktop's mcpServers config), which invokes this script directly by
path rather than via `python -m` from the project directory. Ensures the
project root is importable regardless of the caller's working directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
