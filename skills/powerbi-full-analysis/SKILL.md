---
name: powerbi-full-analysis
description: Use when analyzing, auditing, diagnosing, or reviewing a Power BI Desktop file with the powerbi-desktop plugin's tools -- triggers on "analyze this Power BI file", "audit the data model", "fully analyze my dashboard", "is this dashboard efficient/correct/effective", "why is refresh slow", "why is data missing", "check SQL / SharePoint / OneDrive connections", "does this fold", "what does this file connect to", "who can see what data", "compare these two dashboards", or "diagnose Power Query problems" whenever the powerbi-desktop MCP tools (audit_dashboard, list_open_files, list_power_query, etc.) are available.
version: 3.1.0
---

# Power BI Desktop full analysis

The powerbi-desktop plugin exposes 30 tools. Three kinds:

- **Live tools** (Power BI Desktop must be open with the file loaded; first
  argument is `instance`): everything except the four below.
- **Offline tools** (read files on disk, no Power BI Desktop needed):
  `find_pbix_files`, `get_report_layout`, and `read_mashup_traces` when
  called without an `instance`. `compare_dashboards` works on two `.pbix`
  paths offline (report layout only) or two open files (full model diff).
- **Bridge tool**: `find_unused_fields` needs a live `instance` AND a
  `pbix_path` (`audit_dashboard` fills the path in for you).

Always call `list_open_files()` first, every session -- never reuse an
`instance` from earlier in the conversation (the port changes whenever the
file is reopened). Check each entry's `model_loaded`: `false` means Power BI Desktop is running
with no report loaded (closed, still opening, or a dialog is open) -- ask the
user to open the file and wait for it to finish loading; the same name can
appear twice (a dead engine plus the real file), so use the entry with
`model_loaded: true`, or pass its `port` as `instance`. If the list is empty,
ask the user to open the file, or do
an offline pass with `find_pbix_files()` (use each entry's `path`) + `get_report_layout()`.

## Start here: `audit_dashboard(instance)`

One call runs 15 checks (refresh health, DAX validity, relationships, RLS,
connections, embedded SQL, query folding, SharePoint patterns, dependency
graph, report filters/hidden pages, unused fields, model size, relationship
data integrity, data-quality probes, Power BI's own runtime logs) and returns
ONE ranked list:

- `summary` (counts by severity), `top_priorities`, `findings` (each has
  `severity` high/medium/info, `category`, `message`, and `source_tool` --
  the tool to call for full detail), `by_category`.
- `checks_run` / `checks_failed`: a check that errors is reported there and
  never hides the rest. Report any `checks_failed` to the user.
- `not_checked`: what this audit cannot see. **Always relay this list.**

Lead your write-up with `top_priorities`, then drill into the tool named in
each finding. Don't re-run every individual tool after an audit unless the
user asks for depth in an area.

## Diagnosing the three primary data paths

The user's dashboards load from **SQL Server, SharePoint, and
SharePoint/OneDrive files**. For questions about loading/refresh/connections:

1. `classify_connections` -- every source classified (on-prem SQL needing a
   gateway, Azure SQL, SharePoint site/list, personal OneDrive, web file,
   local or UNC path...), with parameters resolved to their real values.
   Flags: localhost server, non-production names, OneDrive-*synced* local
   paths (break for other users/service), personal OneDrive as a source,
   `http://`, dynamic URLs, the same site spelled several ways.
2. `analyze_sql_queries` -- lints every embedded native SQL statement: writes
   to real tables (temp tables excluded), `TOP N` caps that silently truncate,
   `SELECT *`, no `WHERE`, `CAST(... AS varchar)` with no length (truncates to
   30 chars), year-specific table names/literals that go stale, `NOLOCK`,
   `UNION` vs `UNION ALL`, cross-database reads, `EXEC`.
3. `analyze_folding_risk` -- static, per-step estimate of where query folding
   ends and what then runs locally on fully-extracted data (native SQL never
   folds; `Table.Buffer`; cross-source merges; filters applied *after* folding
   ended so the database sent every row). Pass `include_steps=True` for the
   per-step verdicts. This is an estimate from M text, not proof.
4. `analyze_sharepoint_patterns` -- SharePoint list/file pulls: the same list
   loaded by several queries, `SharePoint.Files` whole-site listings then
   filtered, no column/row reduction after load, hardcoded list GUIDs,
   navigation-blast-radius (one site feeding many tables), and **swallowed
   errors** (`try ... otherwise null` turns a failed source into silently
   empty/blank data).
5. `check_refresh_dependencies` -- source impact (which loaded tables die if
   one server/site goes down), query dependency graph, orphan queries,
   circular references, refresh order fan-out.
6. `read_mashup_traces(instance)` -- the **only** connector-level runtime
   evidence: Power BI's own mashup trace logs. Per SharePoint list: request
   counts, HTTP failures (500/400...), durations; per SQL command: text sent,
   seconds, rows read; engine-reported folding failures; container crashes.
   `since_minutes` (default 1440) bounds the window. The logs cover every file
   opened in that Power BI session, so passing `instance` matters for
   attributing list IDs to this model's queries; failures attributed to other
   files are labelled as such.

## Data-correctness probes

- `check_relationship_integrity(instance)` -- counts key values with no match
  across every relationship. Many-to-one orphans show under (Blank) and drop
  out of cross-filtered measures with no error -- the classic "totals don't
  match" cause. Distinguish a data problem from a wrong-column/format
  mismatch: near-100% unmatched usually means wrong key or a type/format
  mismatch (e.g. `2425` vs `24-25`).
- `profile_table(instance, table, ...)` -- blanks, distinct counts, min/max,
  longest text, entirely-blank/constant columns, text sitting at a truncation
  limit, placeholder dates. Pass `key_columns` for duplicate-key checks and
  `expected_row_count` (a `COUNT(*)` the user ran at the source) for a
  reconciliation check. It reads the loaded model, never the source.
- `compare_dashboards(left, right)` -- each is an open file name or a `.pbix`
  path. Tables, columns, measures (DAX), relationships, RLS roles, Power Query
  step-level diffs, plus report pages/visuals/filters. Use for
  backup-vs-current or before/after a change.

## Structure, logic and efficiency tools

- `get_model_summary`, `list_tables` (split on `IsHidden`), `list_columns`,
  `list_measures`, `list_relationships` (prefer `analyze_relationships`, which
  adds many-to-many/bidirectional/inactive flags and isolated tables),
  `list_hierarchies` (often empty; that's normal).
- `list_all_dax_expressions` -- all DAX in one place. Check `broken_measures`
  first: the engine itself considers those broken.
- `list_power_query` -- each table's M and every shared/staging query broken
  into named steps with `references`. `parsed: false` means this parser
  couldn't split it -- read `raw`, don't treat it as a data problem. Check
  `incremental_refresh_range` (non-null: older partitions won't reflect
  source changes until their turn comes).
- `diagnose_power_query` -- `error_count > 0` means go straight to that
  table's `error_message`; `stale_count` / `refreshed_before_last_edit` means
  the query was edited after the last refresh so loaded data is out of date.
  Treat `state` as an opaque integer; `error_message` is the reliable signal.
- `list_security_roles` -- RLS. Report each role's `unfiltered_tables` and any
  filter `error_message`.
- `list_data_sources` -- distinct sources and which queries use each.
- `analyze_model_size` -- exact per-table VertiPaq bytes; per-object list is
  best-effort. Cross-check big columns against `find_unused_fields`.
- `get_report_layout(pbix_path)` -- pages, visuals, fields, and filters at
  report/page/visual level (`contains_negation: true` = silently excludes the
  listed values). Also a top-level `summary`: hidden pages, drillthrough and
  tooltip pages, hidden visuals, off-canvas visuals, bookmarks, slicer sync
  groups. Works on both the legacy `Report/Layout` and PBIR formats.
- `find_unused_fields(instance, pbix_path)` -- visible columns/measures no
  visual, filter or DAX/RLS references, using exact `Table[Column]` matching.
  Also lists fields used *only* by other model DAX. Leads, not a delete list:
  it can't see tooltips/Q&A/external reports connected to the model.
- `find_query_diagnostics` -- reads Power BI's built-in Query Diagnostics
  output only if the user recorded a session (manual UI feature; the plugin
  cannot start it). If `found_anything` is false, tell the user the
  `how_to_record` steps once and move on.
- `run_dax_query(instance, dax_query, max_rows)` -- returns
  `{rows, row_count, truncated}`. If `truncated`, never conclude "that's
  everything": aggregate in DAX or raise `max_rows`.
- `get_table_row_count`.
- `find_pbix_files` -- recursive scan of Desktop/Documents/Downloads and the
  OneDrive team library; returns `{files, total_found, truncated, searched,
  missing_dirs}`. If `truncated`, raise `max_results` or narrow `search_dirs`.

## Honest limits -- state these, don't paper over them

- **SharePoint folding/paging cannot be proven.** Query Diagnostics leaves
  Data Source Query/Resource/Row Count empty for SharePoint, and trace logs
  don't record request bodies. `read_mashup_traces` shows request counts,
  statuses and timings -- real evidence of failures and repeated fetches --
  but never assert "this filter folded" or "this paged N times".
- `analyze_folding_risk`, `analyze_sql_queries`, `analyze_sharepoint_patterns`
  are static reads of M/SQL text: findings are leads with reasons.
- `find_unused_fields`, `profile_table`, `check_relationship_integrity`
  read the *loaded* model, not the source systems.
- Bookmarks, hidden visuals, tooltip pages and off-canvas detection exist but
  have not been seen against a real example; say "none detected", not "none
  exist".
- An offline `.pbix` normally does not expose its Power Query text (only the
  compressed model has it); compare_dashboards uses the live engine for M.
- Leftover Query Diagnostics queries (`Diagnostics_*`,
  `<Query>_<Step>_Detailed_<timestamp>`) are excluded from findings; if the
  user's model contains them, suggest deleting them before publishing.

## "Fully analyzed" checklist

Before calling an analysis complete, be able to answer:

- **Structure:** tables (hidden vs business-facing), relationships (M2M,
  bidirectional, inactive, isolated).
- **Correctness:** what every measure/column/table computes; broken DAX;
  refresh errors and stale queries; relationship orphans; blank/truncated
  columns.
- **Hidden data:** negated report/page/visual filters, hidden pages, RLS
  tables with no filter, swallowed M errors, `TOP N` caps, incremental
  refresh staleness, year-specific SQL.
- **Effectiveness:** what pages/visuals show, fields nothing uses.
- **Connections:** what each source is, whether it needs a gateway, whether it
  is a dev/personal/synced-path source, blast radius of one source failing.
- **Efficiency:** biggest tables/columns, folding breaks, repeated SharePoint
  fetches, rows read from SQL, failed requests in the trace logs.
- **What you could not see** (the audit's `not_checked` plus the limits above).
