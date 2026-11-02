# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [4.1.0]

### Added
- **Learning loop** (`pbi_mcp/plus/learning.py`, 7 tools): every tool call is logged locally as name, success, duration and a redacted error pattern; failed tool errors get a "past note" hint when a matching lesson exists; `record_lesson`/`get_lessons` keep verified fixes; `sync_knowledge_base`/`search_knowledge` pull and search the public Power BI Knowledge Base (allow-listed repositories, text files only, size limits); `improvement_report` ranks recurring errors and drafts issue text; `forget_learning` deletes it all. On by default, local only, `PBI_MCP_LEARN=0` turns it off, `PBI_MCP_LEARN_DIR` moves it, `PBI_MCP_KB_REPOS` allows more repositories.
- Skill `powerbi-self-improvement`, `docs/SELF-IMPROVEMENT.md`, `docs/knowledge-base-template/`.

### Changed
- `PbiMCPServer.call_tool` records calls and appends past-note hints; behavior is otherwise unchanged and the hook never raises.
- `PRIVACY.md` documents the local learning log.

## [4.0.0]

Merges the standalone "powerbi-desktop-plus" work into this repository: one server, 58 tools, 11 skills.

### Added
- **Offline project engine** (`pbi_mcp/plus`, 25 tools) for saved `.pbip` projects, no Power BI Desktop needed: lossless TMDL parser/editor, PBIR report reader/writer, DAX reference scanner, whole-project validation (DAX102/DAX103, TMDL, PBIR rules), best-practice scan, DAX lint, measure dependency graph, unused-field finder, source classification, Power Query diagnosis, Markdown documentation, project diff, new-project scaffold, project copy.
- **Plan/apply editing** (`plan_model_change`, `plan_report_change`, `apply_change`, `list_project_backups`, `restore_backup`): diff with predicted new errors, backup outside the project, atomic write, validation gate, automatic rollback, restore preview. Operations: measures, calculated columns, properties, relationships, RLS roles, M sources, shared expressions, rename and move (rewriting DAX, relationships, RLS and report visuals), pages, visuals, alt text, themes.
- **Live measure writes** to an open Desktop file through the Tabular Object Model: `validate_dax_live`, `upsert_measure_live`, `delete_measure_live`, `undo_live_change` (dry run first, single-use confirm token, stale-undo protection, dynamic formats preserved).
- `plugin_status` health tool; `PBI_MCP_READONLY` and `PBI_MCP_BACKUP_DIR` switches.
- Six new skills (`powerbi-project-authoring`, `powerbi-dax-authoring`, `powerbi-model-design`, `powerbi-performance-tuning`, `powerbi-troubleshooting`, `powerbi-documentation`), a skill registry, and upgraded versions of the five existing skills.
- Multi-client support: `gemini-extension.json`, `GEMINI.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `docs/clients/` configs, `scripts/install_skills.py`, `docs/CLIENTS.md`.
- 54 pytest tests for the new engine (`tests/`), run in CI; `evals/` trigger cases and tasks; `docs/RESEARCH.md`.

### Changed
- The 3.2.0 file-editing tools (`review_file`, `propose_change`, `finish_change`) stay for `.pbix`, `.pbit`, `.rdl`, `.pbids` and loose files; skills now route between them and the project engine.
- The Microsoft 365 Copilot (HTTP) build is unchanged: it does not expose the new write tools.

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
