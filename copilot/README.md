# Power BI Desktop Analyzer for Microsoft 365 Copilot

The same Power BI analysis as the Claude plugin, packaged as a **declarative
agent with an MCP plugin** for Microsoft 365 Copilot, following Microsoft's
[plugin](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-plugins),
[MCP plugin](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins)
and [plugin manifest 2.4](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-manifest-2.4)
documentation.

Nothing here changes the Claude plugin. It reuses the same analysis code.

## Why this is more than a repackaging

Claude launches the server as a local process and talks to it over stdin/stdout.
**Microsoft 365 Copilot cannot do that.** Its `RemoteMCPServer` runtime calls an
MCP server "hosted outside of Microsoft 365" over public HTTPS (Streamable HTTP).
But this server has to run on the PC where Power BI Desktop is open, because it
reads Power BI's local Analysis Services engine on `localhost`. So the Copilot
build adds three things:

```
Microsoft 365 Copilot ──HTTPS──> dev tunnel ──> 127.0.0.1:8765 ──> this server ──> Power BI Desktop
 (Entra SSO token)              (anonymous;      validates the token,
                                 not a boundary)  tools (edits need an on-screen code)
```

| | Claude plugin | Copilot build |
|---|---|---|
| Transport | stdio, local process | Streamable HTTP on `127.0.0.1`, published by a tunnel |
| Tools | 30 (+3 edit tools) | **11** (Microsoft: quality degrades past ~10 tools per plugin). Same capabilities: related tools are grouped behind an `aspect` / `check` argument |
| Workflow guidance | `skills/powerbi-full-analysis/SKILL.md` | The declarative agent's `instructions` (`appPackage/declarativeAgent.json`) |
| Auth | none (local) | Microsoft Entra SSO; the server validates every token |
| Confirmation prompts | n/a | Tools are annotated `readOnlyHint`, so Copilot doesn't prompt per call after the first-use consent |
| Entry point | `run_server.py` | `run_http_server.py` |

New files: `pbi_mcp/copilot_server.py` (the 11 tools), `pbi_mcp/http_auth.py`
(token validation), `pbi_mcp/http_app.py`, `run_http_server.py`, and this folder.

## The 11 tools

`find_dashboards` (replaces `list_open_files` + `find_pbix_files`), `audit_dashboard`, `inspect_model`,
`inspect_power_query`, `check_data_quality`, `get_report_layout`,
`compare_dashboards`, `run_dax_query`, plus the editing tools `review_file`, `propose_change`
and `finish_change` (see [../docs/EDITING.md](../docs/EDITING.md)).

| Tool | Covers these Claude-plugin tools |
|---|---|
| `inspect_model(aspect)` | get_model_summary, list_tables, list_columns, list_measures, analyze_relationships, list_all_dax_expressions, list_hierarchies, list_security_roles, list_data_sources, analyze_model_size, get_table_row_count |
| `inspect_power_query(aspect)` | list_power_query, diagnose_power_query, find_query_diagnostics, classify_connections, analyze_sql_queries, analyze_folding_risk, analyze_sharepoint_patterns, check_refresh_dependencies, read_mashup_traces |
| `check_data_quality(check)` | check_relationship_integrity, profile_table, find_unused_fields |

The rest map one to one. `list_relationships` is folded into `aspect=relationships`
(which is `analyze_relationships`, a superset).

Because Copilot truncates large tool output, results are compact JSON trimmed to
24,000 characters (`PBI_MCP_MAX_RESPONSE_CHARS`). When something is cut, the
result carries a `_truncated` block and the agent is instructed to say the data
is partial and narrow the request.

## Security: read this before exposing anything

The tunnel must allow **anonymous** access, or Copilot can't reach it (Microsoft's
[debugging guide](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-debug-local)
says so, and that it is "unrelated to any authentication required by your API").
Anything that can reach the tunnel URL can therefore reach this PC's Power BI
data unless the server itself says no. It does:

- **Token validation on every request** (`pbi_mcp/http_auth.py`): RS256 signature
  against your tenant's keys, expiry, issuer + `tid` = your tenant, audience, a
  delegated user token carrying the required scope. `alg: none`, HS256 and
  other-tenant tokens are refused.
- **A mandatory user allow-list.** The server won't start without
  `--allowed-user` (or the explicit `--allow-any-tenant-user`). Without this,
  anyone in the tenant could use their own token to read *your* open files.
- **Loopback bind only**, plus Host-header checking (`--public-host`).
- **Read-only inputs.** Because `readOnlyHint` removes the per-call prompt, the
  inputs are constrained: `run_dax_query` accepts only `EVALUATE`/`DEFINE` DAX
  (the engine would otherwise pass DMV, MDX or TMSL text straight through), file
  arguments must be `.pbix`/`.pbit`, `find_dashboards` stays inside your profile,
  and DAX results are capped at 500 rows (`PBI_MCP_MAX_DAX_ROWS`).
- **An audit trail:** each accepted request logs the caller's identity, each
  refusal logs why (never the token). Copilot interactions also appear in
  Microsoft Purview under the `Copilot` workload.

**Student and staff data.** Tool results, including whatever `run_dax_query`
returns, travel through the tunnel to Microsoft 365 Copilot. Check that this is
allowed under your organization's data-privacy rules (for example FERPA or GDPR) *before* pointing
it at a model that contains student records. The agent instructions tell it to
prefer aggregates, but that is guidance to a language model, not enforcement.

## Easiest route: a no-code agent that reads snapshots (no tunnel, no Entra app, no IT approval)

If you already have an agent you can edit in Microsoft 365 Copilot (Agent Builder) or Copilot Studio, you don't need
anything below. A no-code agent can't call tools on your PC, but it can read files in SharePoint. So:

1. `copilot\export-snapshot.cmd` (with the dashboard open in Power BI Desktop) writes about 15 small `.txt` files
   per dashboard to `Add-In Tools\Power BI Snapshots\<dashboard>\` (audit, model, DAX, Power Query, connections,
   security, report layout, data quality). Nothing reads table rows.
2. The agent's instructions are `copilot/agent/instructions.txt` (about 5,500 of the 8,000 characters allowed);
   description and starter prompts are in the same folder.
3. Point the agent's knowledge at the snapshot folder. Re-run the export after each change.

It can inspect, explain, debug and propose fixes from the snapshot. It cannot run DAX, see live data or edit a file.
Files are `.txt` because Agent Builder accepts only doc/docx/pdf/ppt/pptx/txt/xls/xlsx (not `.md` or `.json`),
split to at most 30,000 characters (Microsoft advises about 36,000), with no tables (Copilot can't parse them). An agent
can use up to 100 SharePoint files, so about six dashboards at a time. Files with *confidential* sensitivity labels
can't be indexed.

Options: `--instance NAME`, `--pbix FILE` (layout only, Power BI closed), `--out FOLDER`, `--no-quality`, `--traces`,
`--max-chars`. A full path must stay under about 260 characters (Windows/OneDrive limit); the exporter checks.
Values typed into the dashboard itself (filter values, constants in DAX/M) appear in the snapshot; obvious
credentials in code are replaced with `[REDACTED]`.

The sections below are the heavier route (live tools inside Microsoft 365 Copilot), which needs a tunnel, an Entra
app and Teams upload rights.

## Quick start: guided setup (live tools; usually needs IT approval)

```
copilot\setup-copilot.cmd
```

It uses the work account you sign in with, and asks before it installs or creates
anything. It downloads Microsoft's dev tunnel tool (signature-checked), signs in
and creates the tunnel, installs the Azure CLI into a private folder (separate from
any `az` you already use), creates the Entra app registration through Microsoft
Graph, walks you through the one Teams-portal form Microsoft has no API for (each
value is copied to your clipboard), starts the server, builds and validates the
package, and helps you upload it. It needs no admin rights, winget or client secret.

| Command | What it does |
|---|---|
| `setup-copilot.cmd` | First-time setup. Safe to re-run: finished steps are skipped. |
| `setup-copilot.cmd run` | **Day to day:** starts the tunnel and server and keeps them running (Ctrl+C stops). |
| `setup-copilot.cmd status` | Shows what is done and what setup would do next. |
| `setup-copilot.cmd package` | Rebuilds the package (e.g. if the tunnel address changed). |
| `setup-copilot.cmd reset` | Forgets the saved settings (deletes nothing remote; prints cleanup commands). |

Options: `--yes` (answer yes to confirmations), `--allowed-user <upn>` (add another
allowed user; you are always included), `--pin-tools`, `--tenant`, `--port`.
Settings (IDs only, no secrets) and logs are in `%LOCALAPPDATA%\powerbi-desktop-mcp\copilot`.
If any automatic step fails (for example your tenant blocks app registration), it
prints the manual instructions for that step, which are the sections below.

## Manual setup

You need: Microsoft 365 with Copilot access (Copilot Chat works; see Microsoft's
[prerequisites](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites)
for licensing), an admin who has enabled **Upload custom apps**, permission to
register an app in Microsoft Entra ID, and the PC running Power BI Desktop with
the file open.

### 1. Install the extra packages

```bash
python -m pip install -r copilot/requirements-copilot.txt
```

### 2. Secure the server with Microsoft Entra SSO

Follow Microsoft's [Entra SSO article](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-entra-sso):

1. In the [Entra admin center](https://entra.microsoft.com/), register an app for
   this server. Note its **client ID** and your **tenant ID**. Under *Expose an
   API*, add a scope named `access_as_user` (the server requires it by default;
   see `--required-scope`).
2. In the [Teams developer portal](https://dev.teams.microsoft.com/tools) go to
   *Tools > Microsoft Entra SSO client ID registration* and register: **Base URL**
   = your MCP URL from step 4, **Restrict usage by org** = your organization,
   **Restrict usage by app** = **Any Teams app**, **Client ID** = the client ID
   above, **Scope** = the scope above. Save it and note the **auth config ID**
   (labelled "Microsoft Entra SSO registration ID") and the **Application ID URI**.
3. Back in Entra: add that Application ID URI to the app's `identifierUris`; add
   `https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect` as a
   **Web** redirect URI; under *Expose an API* add the Microsoft Enterprise token
   store client `ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b` as an authorized client
   application.

Microsoft says steps 2 and 3 are required even if you let Agents Toolkit create
the auth config.

### 3. Start the server

```bash
python run_http_server.py --tenant-id <tenant-guid> --audience "<Application ID URI>" --audience <client-id> --allowed-user <your-upn> --public-host <tunnel-host-from-step-4>
```

Pass both audiences: depending on the app's token version, `aud` is the client id
or the Application ID URI. Options can also come from `PBI_MCP_TENANT_ID`,
`PBI_MCP_AUDIENCES`, `PBI_MCP_ALLOWED_USERS`, `PBI_MCP_PUBLIC_HOSTS`,
`PBI_MCP_REQUIRED_SCOPE`, `PBI_MCP_PORT` (lists comma-separated).
`GET /healthz` is the only unauthenticated path.

### 4. Publish it with a dev tunnel

Install with `winget install Microsoft.devtunnel`, then (per Microsoft's guide):

```powershell
devtunnel user login
devtunnel create --allow-anonymous
devtunnel port create --port-number 8765 --protocol http
devtunnel host
```

The first time, open the **Connect via browser** URL and select **Continue**. The
public address is `https://<id>-8765.<region>.devtunnels.ms`; your MCP URL is that
plus `/mcp`, and the host part is what goes in `--public-host`. A persistent
tunnel keeps its URL across restarts (max lifetime 30 days: `--expiration`).

### 5. Build the app package

```bash
python copilot/build_package.py --mcp-url https://<id>-8765.<region>.devtunnels.ms/mcp --auth-config-id <auth-config-id>
```

This writes `dist/powerbi-desktop-copilot-3.0.1.zip` after validating all three
JSON files against Microsoft's published schemas (manifest 1.25, declarative agent
1.8, plugin 2.4). Options: `--pin-tools` freezes today's tools into the package
instead of runtime discovery (admins then review a fixed tool list; Copilot's
default is dynamic discovery); `--anonymous` builds for a server with no auth
(never with real data).

### 6. Install and use

In Teams: *Apps > Manage your apps > Upload a custom app* and choose the zip
(or `atk install --file-path <zip>`). Then open
[m365.cloud.microsoft/chat](https://m365.cloud.microsoft/chat), pick **Power BI
Desktop Analyzer** under Agents, and try *"Run a full audit of my open Power BI
file."* Copilot asks permission the first time it sends data to the plugin, then
prompts you to sign in. In a licensed Microsoft 365 Copilot session you can type
`-developer on` to see which plugin and tool it picked (Microsoft: developer mode
isn't available elsewhere).

## Verified, and not

**Verified here** (`python -m unittest discover -s copilot/tests`, 136 tests; the snapshot exporter was also run against a
real open model: all 8 sections captured, files under the size limit; the 38
wizard tests use fake `az`/`devtunnel`/console, so nothing real is installed or created):
the wizard's Entra app configuration (idempotent, keeps existing settings, no client
secret), resume/skip behavior, refusal paths and fallbacks. Server side: every
auth failure mode (missing, malformed, wrong-key, `alg: none`, HS256-confusion,
expired, wrong tenant/issuer/audience, missing scope, app-only token, user not on
the allow-list, unknown Host) against the real server; that the tool list is
11 tools (only `propose_change` and `finish_change` are not read-only), flat primitive schemas, no duplicated structured output;
the DAX and path guards; the size-trimming; and that all three package files pass
Microsoft's JSON schemas (and that the validator rejects broken ones). The
wrappers were also run against a real, open Power BI Desktop model on this PC.

**Not verified**, because it needs a tenant, an Entra app and a tunnel that I
don't have: the wizard against the real `az` and `devtunnel` programs (it parses
their output using formats from Microsoft's docs, and logs the raw output); uploading the package; the Entra SSO sign-in flow; the dev tunnel and
Host-header behaviour (if calls fail with 421, add the host Copilot presents to
`--public-host`); and how Copilot's orchestrator actually behaves with these
tools (tool choice, its real truncation limit, confirmation prompts). Expect to
adjust `--required-scope` or the audiences on first contact.

## Limits to know about

- Microsoft's docs say plugins are supported only as actions inside declarative
  agents, not in plain Microsoft 365 Copilot chat. Pick the agent.
- The PC must be on, awake, and running Power BI Desktop with the file loaded, and
  the server and tunnel must be running. A dev tunnel is a development tool, not
  production hosting.
- The placeholders in `manifest.json` (`developer.websiteUrl`, `privacyUrl`,
  `termsOfUseUrl` point at this project's GitHub pages; replace them if you fork) need real URLs before you
  publish the agent beyond your own testing.
- If you rebuild the Claude `.plugin`, exclude this `copilot/` folder (it isn't
  part of that plugin).

## Editing files (approved, reversible)

Supported: `.pbix`, `.pbit`, `.pbip`, `.pbir` (and the `.json` files inside a `*.Report` folder), `.pbids`, `.rdl`, `.tmdl`.
Tools: `review_file` (read), `propose_change` (changes nothing, returns a diff), `finish_change` (`apply` or `rollback`).
Code: `pbi_mcp/editing/`. Tests: `copilot/tests/test_editing.py` (41 tests, synthetic fixtures only).

Setup: create a folder for COPIES (inside your profile, not the profile itself) and start the server with
`PBI_MCP_EDIT_ROOT=<that folder>`. With it unset, editing is off. Originals outside the folder cannot be edited.

Rules the code enforces:
- `propose_change` needs an exact `old_text` that matches the expected number of places, a `reason`, and a result that still
  parses (JSON for pbip/pbir/pbids/Report parts, XML for rdl with no DOCTYPE/ENTITY, indentation/fence check for tmdl).
- `finish_change` needs a 6-digit code that is printed in the server's console window and never returned to Copilot.
  3 wrong tries lock the change; proposals expire after 30 minutes; the file must be unchanged since the proposal.
- Apply takes a full-file backup (`<edit folder>\.pbi-changes\backups\<id>\`), writes only the approved edit, re-reads and
  re-checks it, and restores the original automatically if any check fails. Rollback needs its own code and refuses if the
  file changed after the edit.
- `.pbix`/`.pbit`: only report-layer text parts (`Report/Layout`, `Report/definition/*.json`, and `DataModelSchema` in a
  `.pbit`) can be read or edited. `DataModel`, `DataMashup` and everything else are copied byte-for-byte and never opened.
  Microsoft does not document editing these files outside Desktop (unverified); treat it as experimental and open the
  result in Power BI Desktop. To change the model of a `.pbix`, save it as a `.pbip` and edit the `.tmdl` files.
- Credentials and tokens are never written; the same patterns are shown as `[REDACTED]` by `review_file`.

Not done by this layer: refresh schedules, credentials, publishing, `.rdl` re-publishing (all manual).
Static checks cannot prove a measure evaluates or a visual loads: reopen the file in Desktop and run `audit_dashboard`.
