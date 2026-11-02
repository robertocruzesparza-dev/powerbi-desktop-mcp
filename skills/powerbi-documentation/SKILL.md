---
name: powerbi-documentation
description: "Produces documentation for Power BI projects: data dictionary, measure catalog with DAX, source lineage, relationships, RLS summary, report page and visual inventory, and before/after change comparisons. Use when asked to document a dashboard, hand one over, explain what a report contains, list every measure, show what changed between two versions, or prepare an audit or change log. Do NOT use for analysis that ranks problems (use powerbi-full-analysis)."
metadata:
  version: "1.0"
---

# Power BI documentation

## Procedure

1. Work from a saved `.pbip` (ask the user to Save as `.pbip` from Desktop if they only have a `.pbix`; save first if they have unsaved edits).
2. `document_project {path, output_path}` returns Markdown and saves it when `output_path` is a new `.md` file outside the project folders. It never overwrites.
3. For a change log, `copy_project` before editing, then `diff_projects {left, right}` after: tables, columns, measures (with old and new values), relationships, roles, pages, visuals.
4. Add what the tools cannot know: purpose of each page, owner, refresh schedule, business definitions. Ask the user; do not invent them.

## What the generated document holds

Sources (type and target, no credentials), tables and columns with types and hidden flags, every measure with format, folder, description and DAX, measure dependencies, relationships, RLS filters, report pages with each visual's type, title and fields, and validation and best-practice counts.

## Output for handover

Lead with a half-page summary a non-technical reader can use: what the dashboard answers, who it is for, where the data comes from, how fresh it is, and the known limitations. Put the generated reference after it.

## Gotchas

- Student or staff data: the document holds structure and DAX only, never rows. Keep it that way; do not paste sample values.
- Measures with no description appear blank; list them under "needs a definition" instead of guessing.
- The source classification is a static read of the M text. Say "appears to load from" where it matters.
- `diff_projects` compares saved files; a project open in Desktop with unsaved changes will not show them.
