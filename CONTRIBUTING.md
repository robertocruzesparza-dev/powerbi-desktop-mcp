# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Set up

```bash
git clone https://github.com/robertocruzesparza-dev/powerbi-desktop-mcp
cd powerbi-desktop-mcp
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
python -m unittest discover -s copilot/tests
```

The tests need no Power BI Desktop, tenant or network. Live-model code (`pbi_mcp/engine.py`, `metadata.py`, ...) can only be exercised on Windows with Power BI Desktop open; say in your PR how you tested it.

## Rules

1. **No real data.** Never commit `.pbix`/`.pbit` files, student/employee/customer data, tenant or client IDs, tokens, tunnel URLs or screenshots of real dashboards. Test fixtures are synthetic and generated in code. A test fails the build if private-looking strings are committed.
2. **Edits stay gated.** Any change to `pbi_mcp/editing/` must keep: writes only under `PBI_MCP_EDIT_ROOT`, approval code never returned to the caller, backup before write, verification after, rollback. Add a test for each rule you touch.
3. **Tool descriptions are part of the interface.** Name, when-to-use text and parameter descriptions steer the model. Keep them specific and non-overlapping; update `docs/TOOLS.md`.
4. **Copilot tool count.** Microsoft advises about 10 tools per plugin. The Copilot build is at 11 on purpose; do not add tools there without merging others.
5. **Honest limits.** If a tool cannot prove something, say so in its output and docs. Mark unverified claims `unverified`.
6. Style: Python 3.10+, type hints, small functions and files, no mutation of shared state, errors that say what to do next.

## Pull requests

Describe the problem, the change, and how you tested it. Update `CHANGELOG.md` under *Unreleased*. By contributing you agree your work is released under the MIT license.

## Security

Report vulnerabilities privately; see `SECURITY.md`.
