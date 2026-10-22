"""`powerbi-desktop-mcp` / `python -m pbi_mcp`: start the stdio MCP server (what Claude Desktop,
Claude Code, VS Code and other local MCP clients launch)."""
from __future__ import annotations

import sys

from . import __version__

HELP = f"""powerbi-desktop-mcp {__version__}

Starts the Power BI Desktop MCP server on stdio. MCP clients run this for you; run it by hand only to check
that it starts. It speaks the MCP protocol on stdout, so it prints nothing else there.

  powerbi-desktop-mcp            start the server (stdio)
  powerbi-desktop-mcp --version  print the version and exit
  powerbi-desktop-mcp --doctor   check this PC can run it (Windows, Power BI Desktop, ADOMD.NET) and exit
  powerbi-desktop-mcp-http       the Microsoft 365 Copilot (HTTP) server; see docs/COPILOT.md

Optional environment variables: POWERBI_MCP_SCAN_DIRS (extra folders to search for .pbix files, ';'-separated)
and PBI_MCP_EDIT_ROOT (the folder of COPIES that editing is allowed in; editing is off when unset).
"""


def doctor() -> int:
    """Plain-language checks. Never starts the server and never reads a model."""
    ok = True

    def line(passed: bool, text: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(("OK   " if passed else "FAIL ") + text)

    line(sys.version_info >= (3, 10), f"Python {sys.version_info.major}.{sys.version_info.minor} (needs 3.10 or newer)")
    line(sys.platform == "win32", "Windows (live analysis needs Power BI Desktop, which is Windows-only; file review/editing works anywhere)")
    for module in ("mcp", "psutil"):
        try:
            __import__(module)
            line(True, f"package {module}")
        except ImportError:
            line(False, f"package {module} is not installed (pip install powerbi-desktop-mcp)")
    if sys.platform == "win32":
        for module in ("clr", "win32api"):
            try:
                __import__(module)
                line(True, f"package {module} (pythonnet / pywin32)")
            except ImportError:
                line(False, f"package {module} is not installed")
        try:
            from .discovery import find_adomd_dll

            line(True, f"ADOMD.NET client found: {find_adomd_dll()}")
        except Exception as exc:  # noqa: BLE001 - report whatever went wrong in plain words
            line(False, f"Power BI Desktop's ADOMD.NET client was not found ({exc}). Install Power BI Desktop.")
    print("\nAll checks passed." if ok else "\nSome checks failed. See docs/INSTALL.md#troubleshooting.")
    return 0 if ok else 1


def main() -> None:
    args = sys.argv[1:]
    if "--version" in args:
        print(__version__)
        return
    if "--help" in args or "-h" in args:
        print(HELP)
        return
    if "--doctor" in args:
        sys.exit(doctor())
    from .server import main as run

    run()


if __name__ == "__main__":
    main()
