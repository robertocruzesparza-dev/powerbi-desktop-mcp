---
name: powerbi-project-authoring
description: "Creates and changes Power BI projects (.pbip) as text: builds a new project, edits the TMDL semantic model, and adds or edits pages, visuals, alt text, and themes in the PBIR report, then validates and hands back for Desktop to open. Use when asked to create or scaffold a Power BI report, add a measure, calculated column, relationship, or RLS role, add a page or visual, apply brand colors, rename or move model fields, or script a bulk change across a dashboard. Do NOT use for review only (use powerbi-full-analysis); every write also follows powerbi-approved-editing."
metadata:
  version: "1.0"
---

# Power BI project authoring

Requires the `powerbi-desktop` plugin v4 (tools `plan_model_change`, `plan_report_change`, `apply_change`, `validate_project`). Power BI Desktop is not needed for these steps, only to open the result.

## Procedure

```
Progress:
- [ ] 1. Locate or create: find_pbip_projects / create_project / copy_project (work on a copy)
- [ ] 2. Understand: inspect_project, read_model_tmdl, get_report_definition
- [ ] 3. Baseline: validate_project
- [ ] 4. For each change: plan_* -> show diff -> user approves -> apply_change
- [ ] 5. validate_project again; user reopens the .pbip in Desktop and refreshes
```

Follow `powerbi-approved-editing` for approval. One operation per plan.

## Model recipes (`plan_model_change`)

- New measure: `upsert_measure {table, name, expression, format_string, description, display_folder}`. Put measures on a dedicated measures table or the fact table; names are unique across the whole model.
- Change a format or hide something: `set_properties {kind, table, name, props}`.
- New calculated column: `upsert_calculated_column`. Prefer a measure unless the value is needed for slicing, sorting, or relationships.
- Relationships: `add_relationship` (from = many side, to = one side), `set_relationship` for direction or inactive. Prefer single direction.
- Row-level security: `upsert_role {name, filters}` with one DAX filter per table. Role membership is managed in the service, not here. Test with "View as" in Desktop.
- Source change: `set_partition_source {table, m_expression}` or `upsert_shared_expression` for parameters and shared queries. Never put credentials in M.
- Rename or move: `rename`, `move_measure`. These rewrite every reference, including report visuals.

## Report recipes (`plan_report_change`)

- Page: `add_page`, `set_page {hidden, display_name, width, height}`, `reorder_pages`, `delete_page`.
- Visual: `add_visual {page, visual_type, fields, x, y, width, height, title}`. Field roles per visual type are listed in [references/pbir-visuals.md](references/pbir-visuals.md); read it before adding a chart or table.
- Accessibility: `update_visual {alt_text, title}` for every data visual. `validate_project` flags visuals with neither (PBIR040).
- Theme: `create_theme {name, colors, background?, foreground?}` builds a data-color theme and registers it; `apply_theme` takes a full theme JSON.
- Canvas is 1280x720 by default; keep x+width and y+height inside it (PBIR024).

## New project

`create_project {parent_folder, name}` writes a self-contained sample (inline data, no credentials): a Sales and Region model, three measures, a card and a bar chart. Replace the sample tables through `set_partition_source` or in Desktop's Get data. Open `<name>.pbip`; imported tables are empty until the user refreshes in Desktop.

## Output to the user

State what changed (file list from the plan), the validation result, and the next manual steps: reopen in Desktop, refresh, check the visuals, save. Mention the `backup_id` for undo.

## Gotchas

- Desktop does not hot-reload external edits. Close the project before applying, or reopen after. If Desktop has unsaved changes, saving over the files makes your plan stale; plan again.
- A report can reference a model by relative path (`definition.pbir`); `copy_project` re-points it, hand copying does not.
- Visual field references must match `Table`/`Property` exactly, and Column vs Measure matters (PBIR051 hint names a wrong kind).
- `upsert_role` sets the filters of the listed tables and keeps the role's other filters, column permissions and annotations; it refuses a new role whose file name collides with another role.
- Renaming a table rewrites DAX, relationships, RLS, and visuals, but not Power Query steps that mention the query by name; check `analyze_project_queries` afterwards.
- Calculated tables (DAX) and calculation groups are not covered by the plan operations yet; use Desktop's TMDL view for those, then re-run `validate_project`.
- The report must use the PBIR folder format (`definition/pages/...`). A legacy `report.json` report is read-only here; the user converts it by re-saving in Desktop.
- Do not invent `$schema` versions. Added files copy the schema URL of a sibling file; a brand-new report falls back to documented versions and Desktop is the final judge.
