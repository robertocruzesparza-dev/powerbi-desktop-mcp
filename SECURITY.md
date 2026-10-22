# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's *Report a vulnerability* (private security advisory) on this repository. Include the version (`powerbi-desktop-mcp --version`), steps to reproduce and impact. Do not attach real dashboards or data.

You should get an acknowledgement within 7 days. This is a volunteer project; there is no bounty.

## Supported versions

The latest release receives fixes.

## Threat model in one page

The server gives an AI assistant read access to Power BI models on your PC, and, if you enable it, write access to copies of Power BI files. The assistant can be steered by text it reads (file contents, measure names, error messages): treat all tool output as untrusted.

| Risk | Control |
|---|---|
| Assistant is tricked into editing an original | Writes only inside `PBI_MCP_EDIT_ROOT`; absolute paths resolved; symlinks, the change store and non-Power-BI files refused |
| Assistant approves its own edit | Approval needs a 6-digit code printed only in the server's console; it is never returned to the assistant; salted hash stored; 3 tries; 10-minute code life; 30-minute proposal life |
| Silent or partial damage | Full-file backup first; edit replays only the approved diff; file must be unchanged since proposal; post-write re-validation with automatic restore; rollback refuses to erase later work |
| Non-DAX text run through the DAX tool | Copilot build accepts only `EVALUATE`/`DEFINE` queries and caps rows |
| Credentials leaking into files or output | Credential-like text in an edit is refused; `review_file` redacts it |
| Unauthorized caller of the HTTP build | Entra token validated on every request (signature, expiry, tenant, audience, delegated scope), mandatory user allow-list, loopback bind, Host-header check |
| Tunnel exposure | A dev tunnel must be anonymous for Microsoft 365 Copilot to reach it: the server's token check and allow-list are the only barrier. Use it only for development, with a narrow allow-list |

Known limits: the approval code protects against an assistant, not against malware already running as you. `.pbix`/`.pbit` edits are experimental. Static validation cannot prove a file still opens in Power BI Desktop.

## For contributors

Never commit real dashboards, tokens, tenant or client IDs, tunnel URLs or personal data. Test fixtures must be synthetic.
