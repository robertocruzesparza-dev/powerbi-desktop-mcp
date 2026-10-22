# Install

## 0. Check the PC

After installing (any route below), run:

```bash
powerbi-desktop-mcp --doctor
```

It reports Python version, Windows, required packages and whether Power BI Desktop's ADOMD.NET client is found. It never reads a model.

## A. pip + any MCP client

```bash
pip install powerbi-desktop-mcp
# from source:  pip install git+https://github.com/robertocruzesparza-dev/powerbi-desktop-mcp
```

Use a virtual environment if you prefer: `python -m venv .venv && .venv\Scripts\activate`.

Register the command `powerbi-desktop-mcp` (stdio) in your client.

**Claude Desktop**: add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "powerbi-desktop": {
      "command": "powerbi-desktop-mcp",
      "env": { "PBI_MCP_EDIT_ROOT": "C:\\Users\\you\\PowerBI-Copies" }
    }
  }
}
```

If the executable is not on `PATH`, use the full path (`where powerbi-desktop-mcp`) or `"command": "python", "args": ["-m", "pbi_mcp"]` with the interpreter you installed into. On one tested Claude Desktop install the app rewrote this file and dropped keys it did not recognise; if that happens, use route B.

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "powerbi-desktop": { "type": "stdio", "command": "powerbi-desktop-mcp" }
  }
}
```

**Claude Code**: `claude mcp add powerbi-desktop -- powerbi-desktop-mcp`

Omit `PBI_MCP_EDIT_ROOT` to keep editing off.

## B. Claude plugin (Windows, self-contained)

This repository is a Claude plugin: `.claude-plugin/plugin.json` is the manifest, `.mcp.json` declares the server, and `launch.cmd` + `launch.ps1` start it.

1. Clone or download the repository.
2. Add it as a local plugin (Claude Code: `/plugin marketplace add <path-to-clone>`, then install `powerbi-desktop`; Claude Desktop: Settings > Plugins). The exact menu names change between app versions; if your version needs a marketplace wrapper, see Troubleshooting.
3. Optional, to see progress and avoid a timeout on the very first start:

   ```bash
   powershell -ExecutionPolicy Bypass -File launch.ps1 -Setup
   ```

On first start the launcher (a) finds a 64-bit Python 3.10-3.13, or downloads a private Python 3.12 from python.org (SHA256-pinned, no admin rights, no PATH change), (b) builds a private environment in `%LOCALAPPDATA%\powerbi-desktop-mcp`, and (c) installs `requirements.txt`. Later starts add about 2 seconds. Nothing else on the PC is modified.

Needs internet to python.org, pypi.org and pypa.io on the first start (behind a proxy set `HTTPS_PROXY`).

## C. Microsoft 365 Copilot

See [../copilot/README.md](../copilot/README.md). It needs the extras: `pip install "powerbi-desktop-mcp[copilot]"`.

## Environment variables

| Variable | Purpose |
|---|---|
| `PBI_MCP_EDIT_ROOT` | Folder of **copies** that editing is allowed in. Unset = editing off. Must be a dedicated folder inside your profile |
| `POWERBI_MCP_SCAN_DIRS` | Extra `;`-separated folders for `find_pbix_files` (for example a synced SharePoint library) |
| `POWERBI_MCP_HOME`, `POWERBI_MCP_PYTHON`, `POWERBI_MCP_FORCE_EMBED=1` | Launcher only: working folder, use this python.exe, always use the private downloaded Python |
| `PBI_MCP_MAX_RESPONSE_CHARS`, `PBI_MCP_MAX_DAX_ROWS` | Copilot build: output size caps |

## Update and uninstall

- pip: `pip install -U powerbi-desktop-mcp` / `pip uninstall powerbi-desktop-mcp`.
- Plugin: update the clone; a changed `requirements.txt` rebuilds the private environment. Uninstall by removing the plugin and deleting `%LOCALAPPDATA%\powerbi-desktop-mcp`.
- Restart the MCP client after changing the server or its config.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `--doctor` says ADOMD.NET not found | Install Power BI Desktop. If it lives in an unusual place, `pbi_mcp/discovery.py::find_adomd_dll` needs a path added |
| `list_open_files` is empty | Power BI Desktop is not running or no file is loaded. `model_loaded: false` means Desktop is open with no report |
| First start fails or times out | Run `launch.ps1 -Setup` once, then start a new chat. Log: `%LOCALAPPDATA%\powerbi-desktop-mcp\launcher.log` |
| Launcher blocked | Group policy `AllSigned` stops `launch.ps1`. Use route A, or point `.mcp.json` at your own `python.exe` with `run_server.py` |
| "Editing is off" | Set `PBI_MCP_EDIT_ROOT` and restart the client |
| Plugin will not install from a folder | The app may need a marketplace wrapper (`marketplace.json`) around the plugin. Not verified for every app version; use route A |
| Behind a proxy | Set `HTTPS_PROXY` (and `HTTP_PROXY`) before the first start |

Not tested: a fully offline PC, ARM64 Windows, macOS/Linux for the live tools (not possible: they need Power BI Desktop).
