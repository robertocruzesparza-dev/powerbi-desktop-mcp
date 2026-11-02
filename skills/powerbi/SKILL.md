---
name: powerbi
description: "Coordinates Power BI dashboard work: picks between analysis, troubleshooting, tuning, DAX, model design, project authoring, approved editing, documentation, and Copilot packaging. Use for broad or ambiguous Power BI requests such as fixing, auditing, speeding up, rebuilding, creating, or documenting a dashboard, or when it is unclear which Power BI skill applies. Do NOT use for generic charting or BI design with no Power BI file (use dataviz skills or analytics-serving)."
metadata:
  version: "2.0"
---

# Power BI Coordinator

Read the [area registry](../POWERBI-REGISTRY.md), then load only the specialists the request needs.

## Route

| Request | Skill |
|---------|-------|
| Audit or review a file or project | `powerbi-full-analysis` |
| "Visual is blank", "total is wrong", "refresh fails", "file will not open", "it is slow" | `powerbi-troubleshooting` |
| Make it faster or smaller | `powerbi-performance-tuning` |
| Write, fix, explain, or optimize DAX | `powerbi-dax-authoring` |
| Star schema, date table, relationships, RLS, what to model | `powerbi-model-design` |
| Create a project, add pages or visuals, apply a theme, edit the model in a `.pbip` | `powerbi-project-authoring` |
| Any change to a file: the approval and rollback rules | `powerbi-approved-editing` |
| What is in a `.pbix`/`.pbip`/`.tmdl`/`.pbir`/`.rdl`; can it be edited | `powerbi-file-formats` |
| Data dictionary, measure catalog, handover, before/after comparison | `powerbi-documentation` |
| Put the tools in Microsoft 365 Copilot or Copilot Studio | `copilot-powerbi-agent` |

## Process

1. Find out what is available: call `list_open_files` (open files) and `find_pbip_projects` (saved projects). A `.pbix` has no text definition; for edits, the user saves a `.pbip` copy from Desktop (File > Save as).
2. State the file, whether it is an original or a copy, and what "working" means for the user.
3. Analyze first, rank findings by severity, and say what could not be checked.
4. Propose one change at a time with its diff; write only after explicit approval, on a copy.
5. Re-run the analysis and compare with the baseline.

## Gotchas

- Dashboards often hold student or staff data. Use aggregates; never copy rows into notes.
- Refresh, publishing, and tenant settings are never done by these skills; give manual steps.
- A chat keeps the tool list it started with. If a tool is missing, call `plugin_status`, then start a new chat after updating the plugin.
- A saved `.pbip` can differ from the open file: unsaved changes in Desktop are invisible to project tools. Ask the user to save first when comparing.
