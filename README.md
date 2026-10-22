# powerbi-desktop-mcp

An [MCP](https://modelcontextprotocol.io/) server that lets an AI assistant **analyze** Power BI Desktop files and **safely edit copies** of Power BI files, one approved change at a time.

> Not affiliated with or endorsed by Microsoft. "Power BI", "Microsoft 365 Copilot" and "Copilot Studio" are trademarks of Microsoft Corporation.

## What you get

- **30 read-only analysis tools** for an open Power BI Desktop file: the live data model (tables, columns, DAX, relationships, row-level security, VertiPaq size), Power Query M with step dependencies, refresh health, report layout and filters, and deep checks for SQL Server, SharePoint and OneDrive sources (query-folding estimates, embedded-SQL linting, SharePoint anti-patterns, trace logs). `audit_dashboard` runs 15 checks and returns one ranked list.
- **Approved, reversible editing** of `.pbix`, `.pbit`, `.pbip`, `.pbir`, `.pbids`, `.rdl` and `.tmdl` files, on copies only. Every edit is *propose a diff → you approve → one-time code → backup → apply → verify → rollback if needed*. See [docs/EDITING.md](docs/EDITING.md).
- **Bundled agent skills** that teach the assistant the workflow (`skills/`).
- A **Microsoft 365 Copilot** build (HTTP + Entra sign-in) in [`copilot/`](copilot/README.md).

## Requirements

- **Live analysis:** Windows 10/11 (64-bit) with Power BI Desktop (installer or Microsoft Store version). It reads Desktop's local Analysis Services engine, so Desktop must be open with the file loaded.
- **File review and editing** (`review_file`, `propose_change`, `finish_change`, `get_report_layout`): standard-library Python, any OS.
- Python 3.10 or newer, or let the bundled launcher download a private copy (see [docs/INSTALL.md](docs/INSTALL.md)).

## Quick start

Pick one. Details, config files and troubleshooting are in [docs/INSTALL.md](docs/INSTALL.md).

**A. pip (any MCP client)**

```bash
pip install powerbi-desktop-mcp          # or: pip install git+https://github.com/robertocruzesparza-dev/powerbi-desktop-mcp
powerbi-desktop-mcp --doctor             # checks this PC can run it
```

Then register the command `powerbi-desktop-mcp` as a stdio MCP server in your client (Claude Desktop, Claude Code, VS Code, ...).

**B. Claude plugin (nothing to install by hand on Windows)**
Clone this repo and add it as a local plugin. The first start sets up a private Python environment automatically (about 70 seconds, once).

**C. Microsoft 365 Copilot**
Needs a public HTTPS tunnel and an Entra app registration. See [copilot/README.md](copilot/README.md).

Try it: open a `.pbix` in Power BI Desktop and ask *"Run a full audit of my open Power BI file."*

## Editing is off until you turn it on

Set `PBI_MCP_EDIT_ROOT` to a folder that holds **copies** of your files. With it unset, no edit tool can write anything. Originals outside that folder are refused, even if the assistant is tricked into asking.

## Privacy and safety

The server runs on your machine and reads your Power BI models. What you ask the assistant, and what the tools return, goes to the AI provider your MCP client uses. Dashboards often contain student, employee or customer data: prefer aggregates, and read [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md) before connecting a cloud assistant to real data. The project itself collects no telemetry.

## Documentation

| | |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Install, client config, environment variables, troubleshooting |
| [docs/TOOLS.md](docs/TOOLS.md) | Every tool, and what each can and cannot prove |
| [docs/EDITING.md](docs/EDITING.md) | The change protocol, supported formats, safety rules |
| [copilot/README.md](copilot/README.md) | Microsoft 365 Copilot build |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and rules |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## License

[MIT](LICENSE). Third-party components: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
