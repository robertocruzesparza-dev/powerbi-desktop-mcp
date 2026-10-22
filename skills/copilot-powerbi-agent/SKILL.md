---
name: copilot-powerbi-agent
description: "Plans, builds, and packages a Power BI agent for Microsoft 365 Copilot or Copilot Studio: picks the connection path, sets tool and instruction rules, secures access, and lists the admin approvals needed. Use when asked to bring a Power BI plugin or skill into Copilot, publish a Power BI agent to Teams or Microsoft Copilot, add a Power BI MCP server to a Copilot Studio agent, or explain why a cloud agent cannot reach a model open in Power BI Desktop. Do NOT use for general MCP server review (use mcp-integration) or for analyzing a dashboard (use powerbi-full-analysis)."
metadata:
  version: "1.0"
---

# Copilot Power BI Agent

A cloud Copilot cannot call a local stdio server, so first decide how the agent reaches the model. Read [references/connection-paths.md](references/connection-paths.md) before choosing.

## Workflow

```
Progress:
- [ ] 1. Inventory the existing tools; mark each read-only or write
- [ ] 2. Check current Microsoft Learn pages for every time-sensitive fact (preview vs GA, licensing, limits)
- [ ] 3. Pick a connection path (references/connection-paths.md) and state what breaks
- [ ] 4. Build: group tools, write names and descriptions, add auth and input guards
- [ ] 5. Package and test locally with signed test tokens; no real data
- [ ] 6. Write the admin request list; do not work around tenant policy
```

## Defaults

- Prefer the Microsoft-hosted Power BI servers when the dashboards are published and the task fits: Fabric IQ for reading, the Power BI Authoring server for model changes. Use a self-hosted server only for what they cannot do (Desktop models, files on disk, Power Query text, diagnostics).
- Copilot Studio needs generative orchestration for MCP and supports Streamable HTTP only. Microsoft 365 declarative agents use a plugin manifest with a remote MCP runtime.
- Keep the tool list near ten. Group related reads behind one tool with an `aspect` argument; keep flat primitive arguments.
- Tool names carry more weight than descriptions. Each description says what the tool does, when to use it, and what it does NOT do. No two tools overlap.
- Mark read-only tools `readOnlyHint`. Tools that change files must not be marked read-only, so Copilot asks the user before each call.
- Truncate big results and say so (`_truncated`): connector responses over about 500 KB fail.

## Security

- A tunnel that Copilot can reach must allow anonymous connections, so the server is the only barrier: validate the Entra token signature, tenant, audience, scope, and a user allow-list on every request; bind to loopback; check the Host header.
- Entra has no dynamic client registration, so Copilot Studio's OAuth setup needs your own single-tenant app registration. Never put tokens in an `Authorization` header in config.
- Results flow to Microsoft. Student or staff data needs your organization's privacy approval first. The agent instructions can prefer aggregates, but that is guidance, not enforcement.
- Never write credentials into packages, manifests, or logs. Use environment variables and the platform connection manager.

## Admin dependencies

List each as an exact request, never a workaround: Entra app registration and scope; Teams custom-app upload or admin upload; consent; DLP policy allowing the MCP connector; Power BI tenant setting for the hosted MCP endpoints; capacity and XMLA read-write for model writes. If a feature adds write ability, amend any earlier request that said "read-only" before the admin approves.

## Gotchas

- "Copilot Studio agent" and "Microsoft 365 Copilot declarative agent" are different build paths with different manifests. Confirm which one the user means.
- Preview or GA labels, licensing, and limits change. Cite the Microsoft Learn page you read, not memory.
- The hosted Authoring server is stateful and needs the session header echoed on every call; a client that does not will reconnect every time.
- Fabric IQ is read-only and does not return Power Query text, mashup traces, or storage statistics.
- A trial Copilot Studio license cannot publish.
