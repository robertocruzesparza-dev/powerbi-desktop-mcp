"""Run the Microsoft 365 Copilot (HTTP) server from a source checkout:
`python run_http_server.py --help`. Installed users run `powerbi-desktop-mcp-http` instead."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_mcp.http_cli import main  # noqa: E402

if __name__ == "__main__":
    main()
