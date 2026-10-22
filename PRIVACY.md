# Privacy

`powerbi-desktop-mcp` runs on your own computer. This project operates no servers and collects **no telemetry**.

## What the server reads

With Power BI Desktop open: model metadata (table, column and measure names, DAX, Power Query M, relationships, security roles), report layout, refresh state, and, only when you or the assistant run a DAX query or profile a table, query results from the loaded model. It also reads Power BI Desktop's own trace logs when asked. `review_file` reads the text of files you point it at under your user profile.

## Where the data goes

- **To your AI provider.** Everything a tool returns becomes part of the conversation with the AI service your MCP client uses (for example Anthropic for Claude, Microsoft for Copilot). Query results can contain personal data. Prefer aggregates; do not point a cloud assistant at models holding student, employee, patient or customer records unless your organization has approved it.
- **Microsoft 365 Copilot build.** Results travel through the HTTPS tunnel you create to Microsoft 365 Copilot. You are responsible for the tunnel, the Entra app and the user allow-list. See `copilot/README.md`.
- **Nowhere else.** This project sends nothing to its authors.

## What it writes

- Setup logs and the private Python environment in `%LOCALAPPDATA%\powerbi-desktop-mcp` (plugin launcher only).
- With editing on: backups, pending proposals (which contain the old and new text of the edit) and a journal under `<PBI_MCP_EDIT_ROOT>\.pbi-changes\`. The journal holds ids, file names and hashes, not content. Delete the folder when you no longer need rollback.
- Approval codes are printed to the server console only and stored as salted hashes.

## Network access by the launcher

On first start `launch.ps1` may download a private Python from python.org and packages from pypi.org. Nothing else is fetched.

## Contact

Open a GitHub issue (do not include data) or, for security matters, follow `SECURITY.md`.
