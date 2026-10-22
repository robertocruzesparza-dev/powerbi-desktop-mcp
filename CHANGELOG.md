# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [3.2.0]

### Added
- Approved, reversible editing of copies of `.pbix`, `.pbit`, `.pbip`, `.pbir`, `.pbids`, `.rdl` and `.tmdl` files: `review_file`, `propose_change`, `finish_change` (stdio and Microsoft 365 Copilot builds). One-time console approval code, backup, verification, automatic restore, rollback.
- `pyproject.toml`: `pip install powerbi-desktop-mcp`, console commands `powerbi-desktop-mcp` (with `--version`, `--doctor`) and `powerbi-desktop-mcp-http`.
- Bundled skills for the coordinator, file formats, approved editing and Copilot agent packaging.
- Docs: install guide, tools reference, editing guide, privacy and security policies, third-party notices.

### Changed
- Microsoft 365 Copilot build: 11 tools (was 9); `find_dashboards` replaces `list_open_files` + `find_pbix_files`.
- Default `.pbix` search no longer contains any organization-specific folder; add folders with `POWERBI_MCP_SCAN_DIRS`.
- HTTP entry point moved into the package (`pbi_mcp.http_cli`); `run_http_server.py` remains as a wrapper.

### Fixed
- With `mcp` 2.1 and newer, tool errors reached the model only as "Error executing tool <name>". A small server subclass (`pbi_mcp/mcp_base.py`) now passes the explanatory `ValueError` text through, so messages like "Editing is off: set PBI_MCP_EDIT_ROOT" are visible. Tested on `mcp` 2.0.0 and 2.3.0.
- Byte-order-mark-less UTF-16 text (the `Report/Layout` part of a `.pbix`) is decoded correctly.

## [3.1.0]
- Portable launcher: finds or downloads Python on any Windows PC; no machine-specific paths in `.mcp.json`.

## [3.0.0]
- One-call ranked audit and deep diagnosis for SQL Server, SharePoint and OneDrive sources (30 tools).
- Microsoft 365 Copilot build with Entra token validation.
