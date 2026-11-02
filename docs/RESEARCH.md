# Power BI Desktop: how it works and what can be automated

Research notes behind the v4 upgrade. Facts marked (verified here) were exercised against Power BI Desktop 2.159.883 on this PC.

## 1. The three surfaces

| Surface | What it is | How code reaches it |
|---|---|---|
| Open file's engine | Desktop runs a private Analysis Services (VertiPaq) instance per open file on a local port | ADOMD.NET (DAX, `INFO.*`, DMVs) and the Tabular Object Model, both shipped in Desktop's own `bin` folder (verified: `Microsoft.PowerBI.AdomdClient.dll`, `Microsoft.PowerBI.Tabular.dll`) |
| Saved project (`.pbip`) | Plain-text folders: `*.SemanticModel` (TMDL) and `*.Report` (PBIR), plus a pointer file | any text tool; Desktop detects external edits when the project is reopened (verified: Desktop loaded edited TMDL and PBIR) |
| `.pbix` / `.pbit` | Zip; report layer is text, model (`DataModel`) and Power Query (`DataMashup`) are compressed or opaque | read-only for the report layer; convert to `.pbip` through Desktop (File > Save as); no programmatic conversion |

## 2. Languages and what each controls

- **DAX**: measures, calculated columns and tables, RLS filters, user-defined functions, queries (`EVALUATE`). `INFO.VIEW.*` and `INFO.*` expose model metadata. Verified quirk: `INFO.VIEW.MEASURES()` leaves `FormatString` null and shows a set format as a quoted `FormatStringDefinition`.
- **Power Query M**: load logic. Lives in partitions (`source =`) and shared expressions in TMDL. Folding depends on step order and function support; native SQL does not fold.
- **TMDL**: tab-indented model definition, one object per declaration, `///` descriptions, single-quoted names, multi-line expressions indented two levels deeper than the declaration.
- **PBIR**: report definition as many small JSON files with public `$schema`s (report, pages, page, visualContainer, bookmarks).
- **TMSL**: JSON commands for whole objects (tables, relationships, roles). Verified: the local engine accepts JSON commands over ADOMD, but TMSL has no per-measure object, so measure edits use TOM.

## 3. What Microsoft documents about outside tools

- External tools can write to Desktop's model through its local Analysis Services instance; Desktop synchronizes the change to the canvas. Processing (refresh) commands are not supported, and editing a `.pbit` is not supported. Unusual edits can leave the model inconsistent.
- PBIP is the supported way to version-control and programmatically generate or edit models and reports. Desktop detects changes to project files and offers to apply them.
- Microsoft publishes a Power BI modeling MCP server (modeling only: tables, columns, measures, relationships, calculation groups, RLS, perspectives, translations, TMDL import/export; it cannot change report pages) and hosted authoring and read servers for published models. This plugin covers the pieces they do not: Desktop file diagnostics (Power Query text, traces, storage statistics), report-layer reading and editing, and offline project validation.

## 4. Capability matrix of the upgraded plugin

| Task | Open file | Saved project | `.pbix` only |
|---|---|---|---|
| Analyze model, DAX, M, RLS, size | yes (30 tools) | yes (read/validate/BPA/analyze queries) | report layer only |
| Troubleshoot | yes | partly (static) | partly |
| Create new report project | n/a | yes (`create_project`) | n/a |
| Edit measures | yes (live, dry run) | yes (plan/apply) | no: save as `.pbip` |
| Edit columns, relationships, RLS, M source | no | yes | no |
| Rename or move with reference rewrite | no | yes (model + report) | no |
| Edit pages, visuals, themes | no | yes (PBIR) | no |
| Document, diff | partly | yes | no |
| Refresh, publish, credentials | never | never | never |

## 5. Known limits (state them to users)

- Static analysis (BPA, folding, SQL, SharePoint) reads text; it does not measure runtime. SharePoint folding and paging cannot be proven.
- Visual rendering cannot be verified outside Desktop. Verified here: Desktop opened an edited project, showed both pages, and bound a visual to a renamed measure.
- Not yet covered by plan operations: calculated tables, calculation groups, perspectives, translations, marking a date table, bookmarks, conditional formatting, report-level measures.
- PBIR `$schema` versions copy siblings; a brand-new report uses documented fallback versions.
- Live writes were exercised on a scratch file only (create, update, delete guard, undo, readback).

## Sources

- Microsoft Learn: Power BI Desktop projects (overview, report folder / PBIR), External tools in Power BI Desktop, Programming semantic models with TOM.
- github.com/microsoft/powerbi-modeling-mcp (README: tool groups, connection types, limits).
