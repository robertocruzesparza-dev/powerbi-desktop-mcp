---
name: powerbi-file-formats
description: "Explains what each Power BI file type contains, which parts are plain text, and how safely each can be read or edited: .pbix, .pbit, .pbip, .pbir, .pbids, .rdl, and .tmdl. Use when asked to open, inspect, compare, diff, convert, or edit a Power BI file outside Power BI Desktop, to choose between editing a .pbix and a .pbip, or when a Power BI file will not open after an edit. Do NOT use for analyzing a model's quality (use powerbi-full-analysis) or for the approval workflow (use powerbi-approved-editing)."
metadata:
  version: "2.0"
---

# Power BI File Formats

Decide the format before any edit: it sets what is possible and how risky it is.

## Quick map

| Type | What it is | Text? | Edit outside Desktop |
|------|------------|-------|----------------------|
| `.pbip` | Pointer JSON to a project's report and semantic-model folders | Yes | Yes, but a wrong path breaks the project |
| `.tmdl` | Semantic model definition, one object per file (tables, measures, relationships, roles) | Yes, tab-indented | Yes: the preferred way to change a model |
| `.pbir` and `*.Report/definition/**.json` | Report definition: report, pages, visuals, bookmarks | Yes (JSON) | Yes: the preferred way to change pages and visuals |
| `.pbids` | Data-source pointer (server, database, file), no credentials | Yes (JSON) | Yes |
| `.rdl` | Paginated report (XML), with embedded data source and queries | Yes (XML) | Yes; takes effect only after re-publishing |
| `.pbit` | Template: zip with report and model schema, no data | Parts | Report parts and `DataModelSchema` only; experimental |
| `.pbix` | Zip with report, compressed model and data | Only the report parts | Report-layer parts only; experimental |

## .pbix and .pbit

Both are zip containers. List the parts first; do not guess.

- Text and safe to read: `Report/Layout` (legacy format, UTF-16 JSON) or `Report/definition/**.json` (PBIR), and in a `.pbit` `DataModelSchema`.
- Never edit: `DataModel` (compressed binary model with the data), `DataMashup` (Power Query container), `SecurityBindings`, `Metadata`, `Settings`, `Version`. Rewriting them corrupts the file or drops data.
- Rewrite rule: copy every other part byte-for-byte, keep order and compression, then test the zip.
- Microsoft does not document editing these files outside Power BI Desktop. Always work on a copy and open the result in Desktop before trusting it.

To change a `.pbix` model, convert instead of hacking the zip: open the copy in Desktop and use Save as `.pbip`, then edit the `.tmdl` files. Conversion in either direction happens only in Desktop (File > Save as); no tool converts programmatically.

## .pbip projects

A project is a pointer file plus two folders: `<name>.Report` and `<name>.SemanticModel`. Model changes go in `.tmdl` under the semantic-model `definition` folder; report changes go in the report's `definition` folder. Desktop does not hot-reload: reopen the project after each external edit.

## PBIR report folder

`<name>.Report/definition/` holds `report.json` (theme, report filters), `version.json`, `pages/pages.json` (order, active page), one folder per page with `page.json`, and one folder per visual with `visual.json`; optional `bookmarks/` and `reportExtensions.json`. Every file carries a public `$schema`. A legacy report has a single `report.json` at the report root instead and is not supported for outside edits. Field-by-field reference: `powerbi-project-authoring/references/pbir-visuals.md`.

## What the plugin can do per format

| Format | Read | Validate | Edit |
|--------|------|----------|------|
| open file in Desktop | all live tools | `audit_dashboard` | `upsert_measure_live`, `delete_measure_live` (dry run first) |
| `.pbip` + TMDL + PBIR | `read_model_tmdl`, `get_report_definition`, `inspect_project` | `validate_project`, `best_practice_scan` | `plan_model_change`, `plan_report_change`, then `apply_change` |
| `.pbix` | `get_report_layout`, `review_file` (report layer only) | none | `.pbix` report-layer text via `propose_change`/`finish_change` (experimental); for the model, save as `.pbip` first |
| `.rdl`, `.pbids` | `review_file` | static parse check | `propose_change`/`finish_change` on a copy (approval code required) |

Desktop itself ships the Tabular Object Model, so a model open in Desktop can be written through it; that is how the live tools work, and it only changes the open session until the user saves.

## TMDL rules

- Indent with tabs consistently; never mix tabs and spaces in one line's indent.
- Multi-line DAX or M sits in a fenced block; keep fences balanced.
- One object per declaration (`table`, `measure`, `column`, `relationship`); names with spaces are single-quoted, and a quote inside a name is doubled (`'O''Brien'`).
- A multi-line expression sits on lines indented at least two tabs deeper than its declaration; properties sit one tab deeper. A `///` line above an object is its description.
- Descriptions of relationship columns use `Table.Column`; quote each part that needs it.

## Verify after any edit

1. File still parses (JSON, XML, or TMDL structure).
2. Reopen in Desktop; note any repair prompts or errors.
3. Run `powerbi-full-analysis` and compare with the pre-edit result.

## Gotchas

- Mixed tabs and spaces in TMDL structure lines make Desktop reject the file; spaces inside a DAX or M body are fine (`validate_project` knows the difference).
- Deleting a table also needs its `ref table` line in `model.tmdl` removed; `delete_object` does both.
- A `.pbids` file never holds credentials; if one appears, remove it and tell the user.
- An `.rdl` `ConnectionString` can contain a password. Redact it in anything you show or log.
- File names inside a `.pbix` use forward slashes and are case-sensitive.
- A `.pbit` has no data, so refresh behavior cannot be tested from it.
