# Security policy

`powerbi-desktop-mcp` gives an AI assistant read access to Power BI models on your computer and, if you enable it, write access to *copies* of Power BI files. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a suspected vulnerability.**

1. Use GitHub's private reporting: on this repository open **Security > Report a vulnerability**.
2. If you cannot use that, open a public issue titled **"Security contact request"** with *no* technical details. A maintainer will reply with a private channel.

Please include:

- the affected version (`powerbi-desktop-mcp --version`) and install route (pip, plugin, Microsoft 365 Copilot build);
- what you found, the impact, and the smallest steps to reproduce it;
- your operating system, Power BI Desktop version and MCP client;
- how you would like to be credited, or that you prefer to stay anonymous.

**Do not attach real dashboards, student/employee/customer data, tokens, tenant or client IDs, or tunnel URLs.** Use synthetic files. If you have already exposed a real secret while testing, rotate it.

## What to expect

This is a volunteer-maintained project, so these are targets, not guarantees:

| Step | Target |
|---|---|
| Acknowledge your report | within 7 days |
| Initial assessment (valid? severity?) | within 14 days |
| Fix or documented mitigation | critical: 30 days; high: 60 days; medium/low: next release |
| Coordinated disclosure | after a fix is released, or 90 days after your report, whichever comes first; earlier if the issue is already being exploited, later only by agreement |

Confirmed vulnerabilities are published as a GitHub Security Advisory (with a CVE where eligible), described in `CHANGELOG.md`, and credited to the reporter unless they ask otherwise. There is no bug bounty.

## Supported versions

| Version | Supported |
|---|---|
| 3.2.x | Yes |
| Earlier than 3.2 | No: upgrade. 3.2 added the editing layer and moved the HTTP entry point |

## Scope

**In scope** (code and docs in this repository):

- the editing layer (`pbi_mcp/editing/`): writing outside `PBI_MCP_EDIT_ROOT`, bypassing or leaking the approval code, applying an edit that was not approved, backup/rollback failures that lose data, path traversal or symlink tricks, credential handling;
- the Microsoft 365 Copilot (HTTP) build (`pbi_mcp/http_*.py`, `copilot_server.py`): authentication or authorization bypass, token-validation flaws, Host-header or DNS-rebinding issues, the DAX read-only guard, response-size handling;
- the stdio server and analysis tools: unintended code execution, reading files outside the intended scope, reading or writing data the user did not ask for;
- the launcher (`launch.ps1`, `launch.cmd`) and setup wizard: unsafe downloads, missing integrity checks, privilege problems;
- documentation that gives unsafe instructions.

**Out of scope:**

- vulnerabilities in Power BI, Microsoft Entra, Microsoft 365 Copilot, Windows or Claude themselves: report them to the vendor (for Microsoft products, the Microsoft Security Response Center);
- vulnerabilities in dependencies (`mcp`, `pythonnet`, `uvicorn`, `PyJWT`, ...): report upstream; tell us too if our usage makes it worse;
- an attacker who already runs code as the user on the same PC (the approval code protects against an assistant, not against malware with your privileges);
- prompt injection that only makes the assistant *say* something wrong, with no effect on files, data access or approvals;
- findings that need a deliberately unsafe configuration documented as unsafe (for example `--allow-anonymous`, `--allow-any-tenant-user`, or an `PBI_MCP_EDIT_ROOT` set to a folder with originals);
- denial of service from huge files on your own machine, social engineering, and automated scanner output with no demonstrated impact.

## Severity

We use CVSS v3.1 as a guide and adjust for context. Examples for this project:

- **Critical:** write or read outside the edit folder; applying an edit without a valid approval code; authentication bypass on the HTTP build; remote code execution.
- **High:** approval code returned to the assistant or logged; rollback that destroys later work; credentials written into files or logs; token-validation weaknesses.
- **Medium:** information disclosure of model metadata to an unauthorized local process; unsafe defaults in the setup wizard.
- **Low:** hardening gaps, missing validation with no demonstrated impact.

## Safe harbor

If you make a good-faith effort to follow this policy, we will not pursue or support legal action against you for research on this project's code. Good faith means: test only on your own machine, files and tenant; do not access, modify or keep other people's data; do not degrade services you do not own; stop and report once you can show the issue. We cannot speak for Microsoft, Anthropic or anyone else: their systems have their own rules.

## How the design limits damage

| Risk | Control |
|---|---|
| Assistant is tricked into editing an original | Writes only inside `PBI_MCP_EDIT_ROOT`; absolute paths resolved; symlinks, the change store and non-Power-BI files refused |
| Assistant approves its own edit | Approval needs a 6-digit code printed only in the server's console; never returned to the assistant; salted hash stored; 3 tries; 10-minute code life; 30-minute proposal life |
| Silent or partial damage | Full-file backup first; edit replays only the approved diff; file must be unchanged since proposal; post-write re-validation with automatic restore; rollback refuses to erase later work |
| Non-DAX text run through the DAX tool | Copilot build accepts only `EVALUATE`/`DEFINE` queries and caps rows |
| Credentials leaking into files or output | Credential-like text in an edit is refused; `review_file` redacts it |
| Unauthorized caller of the HTTP build | Entra token validated on every request (signature, expiry, tenant, audience, delegated scope), mandatory user allow-list, loopback bind, Host-header check |
| Tunnel exposure | A dev tunnel must be anonymous for Microsoft 365 Copilot to reach it: the server's token check and allow-list are the only barrier. Use it for development, with a narrow allow-list |

## Known limits

- The approval code protects against an assistant, not against malware running as you.
- `.pbix`/`.pbit` edits are experimental; static validation cannot prove a file still opens in Power BI Desktop.
- Tool output is data from your files. A file can contain text that tries to instruct the assistant; the agent instructions tell it to ignore that, but that is guidance to a language model, not enforcement.
- Query results go to your AI provider. Do not connect a cloud assistant to data you are not allowed to share (see `PRIVACY.md`).
- Releases are not currently signed. Install from PyPI or this repository's tagged releases and compare file hashes on the release page.

## Using it safely

- Keep editing **off** (`PBI_MCP_EDIT_ROOT` unset) unless you need it; when on, use a dedicated folder of copies.
- Never run the HTTP build with `--allow-anonymous` or `--allow-any-tenant-user` on a machine with real data. Keep `--allowed-user` to yourself; stop the tunnel when not in use.
- Prefer aggregates in DAX queries; do not paste records into chats.
- Keep Power BI Desktop, Python and this package up to date; run `pip-audit` periodically.
- Read the diff and check the file name before approving; read the approval code from the server window yourself.

## For contributors

Never commit real dashboards, tokens, tenant or client IDs, tunnel URLs or personal data. Test fixtures must be synthetic. Changes to `pbi_mcp/editing/` or the HTTP authentication need a test for each security rule they touch (see `CONTRIBUTING.md`).
