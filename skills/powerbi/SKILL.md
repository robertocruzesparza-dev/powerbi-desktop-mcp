---
name: powerbi
description: "Coordinates Power BI dashboard work: choosing between analysis, file-format handling, approved edits, and packaging a Power BI agent for Microsoft 365 Copilot or Copilot Studio. Use for broad or ambiguous Power BI requests such as fixing, auditing, rebuilding, or documenting a dashboard, or when it is unclear which Power BI skill applies. Do NOT use for generic charting or BI design with no Power BI file (use dataviz skills or analytics-serving)."
metadata:
  version: "1.0"
---

# Power BI Coordinator

Load only the specialist skills the request needs.

## Route

| Request | Skill |
|---------|-------|
| Audit, diagnose, or review a dashboard; why is refresh slow or data missing | `powerbi-full-analysis` |
| What is inside a `.pbix`/`.pbip`/`.tmdl`/`.pbir`/`.rdl`; can this be edited | `powerbi-file-formats` |
| Change a measure, query, relationship, visual, or source | `powerbi-approved-editing` (after `powerbi-full-analysis` for a baseline) |
| Put the Power BI tools in Microsoft 365 Copilot or Copilot Studio | `copilot-powerbi-agent` |

## Process

1. State the file, whether it is an original or a copy, and what "working" means for the user.
2. Analyze first. Report findings ranked by severity and what could not be checked.
3. Propose fixes one at a time; apply only on explicit approval, on a copy.
4. Re-run the analysis after each change and compare with the baseline.

## Gotchas

- Dashboards often hold student or staff data. Use aggregates and never copy rows into notes.
- Refresh, publishing, and tenant settings are never done by these skills; give manual steps.
