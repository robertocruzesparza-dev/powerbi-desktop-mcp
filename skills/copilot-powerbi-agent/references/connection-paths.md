# Connection paths for a Power BI agent

## Contents
- Paths compared
- Choosing
- What each path cannot do
- Verify before building

## Paths compared

| Path | Reads | Writes | Needs | Main risk |
|------|-------|--------|-------|-----------|
| A. Own MCP server on the PC, exposed by a tunnel | Everything a local server can read, including Desktop models, Power Query, diagnostics | Whatever you build (file edits on copies) | Tunnel, Entra app, upload rights | Anonymous tunnel to a machine with sensitive data; server is the only barrier |
| B. Hosted Fabric IQ MCP | DAX, schema, report metadata of published models | None | Published model, Entra consent | No Power Query, traces, or size data |
| C. Hosted Power BI Authoring MCP | Model structure | Model objects only; not report pages | Write permission, tenant setting, XMLA read-write on capacity | Preview status, stateful session |
| D. Snapshot agent (no code) | Exported text files in SharePoint | None | Export step after each change | Stale data; cannot run DAX |
| E. Declarative agent plugin (M365) | As A, B, or C | As the server allows | Manifest, SSO registration, admin approval | Separate toolchain from Copilot Studio |

## Choosing

1. Dashboards published and the task is analysis or model edits: B plus C.
2. Desktop files, Power Query, diagnostics, or report-visual edits: A, with writes limited to a folder of copies and a local approval code.
3. No admin approval available yet: D to get value now.
4. Combine only when each tool set is distinct. Do not register overlapping servers.

## What each path cannot do

- B, C, D cannot read Power Query text, mashup traces, or VertiPaq sizes.
- C cannot change report pages or visuals.
- None of the hosted paths can refresh, publish, or change tenant settings from the agent.

## Verify before building

Fetch the current Microsoft Learn pages for MCP onboarding in Copilot Studio, the Power BI MCP servers overview, Fabric IQ, plugin confirmation prompts, and licensing. Record URL and the facts used. Anything not read this session is "unverified".
