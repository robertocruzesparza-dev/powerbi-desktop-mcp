# Using this with Claude, Gemini and GitHub Copilot

The server is a standard MCP server over stdio, and the skills use the open Agent Skills layout
(`skills/<name>/SKILL.md`), so one repository serves every client. `AGENTS.md` carries the rules for clients
that read it.

| Client | Tools (MCP) | Skills | Context file |
|---|---|---|---|
| Claude Desktop / Claude Code | plugin (`.claude-plugin`, `.mcp.json`) or pip + client config | plugin bundles them, or `install_skills.py --target claude` | `AGENTS.md` |
| Gemini CLI | extension (`gemini-extension.json`) or `docs/clients/gemini-settings.json` | extension `skills/`, or `--target gemini` | `GEMINI.md` |
| GitHub Copilot (VS Code, agent mode) | `docs/clients/vscode-mcp.json` -> `.vscode/mcp.json` | `--target copilot --project <folder>` -> `.github/skills/` | `.github/copilot-instructions.md`, `AGENTS.md` |
| GitHub Copilot CLI | `docs/clients/copilot-cli-mcp-config.json` | `--target copilot` | `AGENTS.md` |
| Microsoft 365 Copilot / Copilot Studio | HTTP build, see [copilot/README.md](../copilot/README.md) | n/a | n/a |

## Install the server once

```bash
pip install powerbi-desktop-mcp        # or: pip install git+https://github.com/robertocruzesparza-dev/powerbi-desktop-mcp
powerbi-desktop-mcp --doctor
```

## Claude

Add this folder as a local plugin, or register `powerbi-desktop-mcp` as a stdio server. Then
`python scripts/install_skills.py --target claude` if you want the skills outside the plugin. Restart the app and ask
for `plugin_status`.

## Gemini CLI

```bash
gemini extensions install https://github.com/robertocruzesparza-dev/powerbi-desktop-mcp
```

This reads `gemini-extension.json` (server + `GEMINI.md` + `skills/`). Alternatively merge
[docs/clients/gemini-settings.json](clients/gemini-settings.json) into `~/.gemini/settings.json`.

## GitHub Copilot

VS Code: copy [docs/clients/vscode-mcp.json](clients/vscode-mcp.json) to `.vscode/mcp.json`, then
`python scripts/install_skills.py --target copilot --project .`. Copilot CLI: merge
[docs/clients/copilot-cli-mcp-config.json](clients/copilot-cli-mcp-config.json) into its MCP config.

## Verification status (be aware)

- Verified here: the server starts over stdio and lists all 65 tools through the MCP protocol; every config file in
  `docs/clients/` and `gemini-extension.json` is valid JSON and is checked by tests; the skills installer is tested.
- Not verified here: a live session in Gemini CLI or GitHub Copilot. Their file locations and config keys follow the
  vendors' published documentation as of this release; if a client has changed them, adjust the path and keep the
  server command the same.
- Live analysis and live measure writes need Windows and Power BI Desktop. Project and file tools run anywhere.
- Skill routing quality differs by client; the `evals/` folder has the trigger cases to test a client with.
- Tool-heavy sessions: some clients cap the number of tools. If yours does, enable only the layers you need.
