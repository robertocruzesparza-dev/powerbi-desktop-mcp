---
name: powerbi-troubleshooting
description: "Diagnoses Power BI problems from a symptom to a proven cause: blank or wrong visuals, totals that do not match, missing rows, refresh errors or stale data, slow reports, SharePoint or SQL load failures, a file that will not open or shows a repair prompt, and RLS showing the wrong data. Use when the user describes something broken or surprising in a Power BI dashboard, model, or project. Do NOT use for a general health review with no symptom (use powerbi-full-analysis)."
metadata:
  version: "1.0"
---

# Power BI troubleshooting

Start from the symptom, collect evidence with read-only tools, name the cause, then hand any fix to `powerbi-approved-editing`.

## Intake

Ask only what the tools cannot tell you: which page and visual, what number is shown, what number is expected and from where, when it last worked, and whether anything changed. Then `list_open_files` (use the entry with `model_loaded: true`) or `find_pbip_projects`.

## Symptom table

| Symptom | First tools | Usual causes |
|---|---|---|
| Visual blank | `get_report_layout`, `run_dax_query` on the visual's measure | field removed or renamed; negated or hidden filter (`contains_negation`); relationship filters out all rows; measure returns BLANK for the context |
| Total differs from the sum of rows | `check_relationship_integrity`, `analyze_relationships` | orphan keys under (Blank); many-to-many; a measure that is not additive; bidirectional filter |
| Missing rows or students | `profile_table`, `read_mashup_traces`, `analyze_sharepoint_patterns` | `TOP N` in SQL; SharePoint list threshold or paging; a `try ... otherwise null` that swallowed a failure; incremental-refresh range; filter applied after folding ended |
| Refresh fails | `diagnose_power_query`, `read_mashup_traces`, `classify_connections` | credentials or privacy levels; gateway needed for on-premises SQL; renamed SharePoint list or column; synced local path that only exists on one PC |
| Data is stale | `diagnose_power_query` (`refreshed_before_last_edit`), `list_power_query` (`incremental_refresh_range`) | query edited since last refresh; older partitions not refreshed |
| Slow report or refresh | `powerbi-performance-tuning` | too many visuals, heavy DAX, folding breaks, large high-cardinality columns |
| Wrong number in a measure | `powerbi-dax-authoring` (debugging procedure) | filter context, relationships, data types |
| Wrong rows for a user | `list_security_roles` | role with no filter, filter on the wrong table, bidirectional relationship leaking filters, user mapping |
| File will not open or shows a repair prompt | `validate_project` | mixed tab and space indent in TMDL; duplicate names; a reference to a missing field; a hand edit to a `.pbix` part; a bad `$schema` |
| Project edit not visible | `list_project_backups`, `plugin_status` | Desktop holds the old files in memory; reopen the project |

## Method

1. Reproduce the number with `run_dax_query` at the smallest grain that shows the problem.
2. Bisect: evaluate each component until one disagrees with expectation.
3. Prove a data cause against the source count the user supplies (`profile_table expected_row_count`); the plugin reads the loaded model, not the source.
4. Rule out report-level causes: hidden pages, report, page, and visual filters, slicer sync groups.
5. State the cause, the evidence, what you could not verify, and the smallest fix.

## Output template

```
Symptom: <as the user described>
Cause: <proven cause or ranked hypotheses with evidence for each>
Evidence: <tool, finding, number>
Not verified: <what only the source system or the service can confirm>
Fix: <one change, see powerbi-approved-editing>
```

## Gotchas

- Never assert a SharePoint filter folded or paged; the plugin shows request counts and failures, not request bodies.
- A `model_loaded: false` entry means Desktop is open with no model (still loading or a dialog open); ask the user to finish opening it.
- Query Diagnostics output tables (`Diagnostics_*`) are leftovers; ignore them and suggest deleting them before publishing.
- Offline `.pbix` parsing reads only the report layer; Power Query text and the model need the live file or a `.pbip` copy.
