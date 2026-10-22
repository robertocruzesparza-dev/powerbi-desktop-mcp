---
name: powerbi-file-formats
description: "Explains what each Power BI file type contains, which parts are plain text, and how safely each can be read or edited: .pbix, .pbit, .pbip, .pbir, .pbids, .rdl, and .tmdl. Use when asked to open, inspect, compare, diff, convert, or edit a Power BI file outside Power BI Desktop, to choose between editing a .pbix and a .pbip, or when a Power BI file will not open after an edit. Do NOT use for analyzing a model's quality (use powerbi-full-analysis) or for the approval workflow (use powerbi-approved-editing)."
metadata:
  version: "1.0"
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

To change a `.pbix` model, convert instead of hacking the zip: open the copy in Desktop and use Save as `.pbip`, then edit the `.tmdl` files.

## .pbip projects

A project is a pointer file plus two folders: `<name>.Report` and `<name>.SemanticModel`. Model changes go in `.tmdl` under the semantic-model `definition` folder; report changes go in the report's `definition` folder. Desktop does not hot-reload: reopen the project after each external edit.

## TMDL rules

- Indent with tabs consistently; never mix tabs and spaces in one line's indent.
- Multi-line DAX or M sits in a fenced block; keep fences balanced.
- One object per declaration (`table`, `measure`, `column`, `relationship`); names with spaces are single-quoted.

## Verify after any edit

1. File still parses (JSON, XML, or TMDL structure).
2. Reopen in Desktop; note any repair prompts or errors.
3. Run `powerbi-full-analysis` and compare with the pre-edit result.

## Gotchas

- A `.pbids` file never holds credentials; if one appears, remove it and tell the user.
- An `.rdl` `ConnectionString` can contain a password. Redact it in anything you show or log.
- File names inside a `.pbix` use forward slashes and are case-sensitive.
- A `.pbit` has no data, so refresh behavior cannot be tested from it.
