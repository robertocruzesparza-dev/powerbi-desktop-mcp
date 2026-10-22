# Tools and known limits

Every analysis tool is read-only. Pass the `instance` that `list_open_files` reports to the live tools.

## Analysis tools (30)

| Tool | Needs PBI Desktop open? | Description |
|---|---|---|
| `audit_dashboard` | yes | **Start here.** Runs 15 checks and returns one ranked findings list, each pointing at the tool for detail; failed checks are reported, `not_checked` lists blind spots |
| `list_open_files` | yes | List every open Power BI Desktop file, with `model_loaded` (false = Power BI is running but no report is loaded) |
| `get_model_summary` | yes | Table/measure/relationship counts + full lists |
| `list_tables` | yes | All tables, with hidden/description metadata |
| `list_columns` | yes | Columns (optionally filtered to one table) |
| `list_measures` | yes | DAX measures with their expressions |
| `list_relationships` | yes | Relationships, cardinality, active/cross-filter |
| `list_all_dax_expressions` | yes | Every measure, calculated column, and calculated table's DAX in one place |
| `list_power_query` | yes | Power Query (M) code, broken into named steps with step-to-step dependencies, plus each table's live refresh state/error |
| `diagnose_power_query` | yes | Fast health check: which tables errored on last refresh (with the error message) and which were edited after their last refresh (`refreshed_before_last_edit`) |
| `find_query_diagnostics` | yes | Reads output from Power BI Desktop's built-in Query Diagnostics feature, if you've recorded a session |
| `analyze_relationships` | yes | Relationships flagged for many-to-many, bidirectional filtering, inactive, and isolated tables |
| `list_security_roles` | yes | Row-level security roles, per-table filter expressions, and tables with no filter at all |
| `list_data_sources` | yes | Every distinct data source the model connects to (SQL servers, SharePoint, URLs, files), and which queries use each |
| `analyze_model_size` | yes | VertiPaq storage byte totals per table (exact) and largest storage objects (best-effort) -- what's bloating the model |
| `list_hierarchies` | yes | User-defined hierarchies (e.g. Year > Quarter > Month) and their levels |
| `find_unused_fields` | yes + path | Visible columns/measures nothing in the report, filters, DAX or RLS uses (exact `Table[Column]` matching), with bytes reclaimable |
| `get_table_row_count` | yes | Row count for a table |
| `run_dax_query` | yes | Run an arbitrary DAX `EVALUATE` query; returns `{rows, row_count, truncated}` |
| `classify_connections` | yes | Every source classified (on-prem/Azure SQL, SharePoint, OneDrive, web, local/UNC), parameters resolved, gateway need, risky patterns (localhost, synced paths, personal OneDrive, http, dynamic URLs) |
| `analyze_sql_queries` | yes | Lints embedded native SQL: writes, `TOP N` caps, `SELECT *`, `CAST` without length, year-specific tables/literals, NOLOCK, UNION, cross-DB reads |
| `analyze_folding_risk` | yes | Static per-step query-folding estimate; flags native SQL, `Table.Buffer`, cross-source merges, filters running after folding ended |
| `analyze_sharepoint_patterns` | yes | SharePoint list/file anti-patterns: duplicate list loads, whole-site `SharePoint.Files`, no reduction, hardcoded GUIDs, blast radius, swallowed errors (`try ... otherwise null`) |
| `check_refresh_dependencies` | yes | Which loaded tables depend on each source, dependency graph, orphan and circular queries |
| `read_mashup_traces` | optional | Reads Power BI's own trace logs: SharePoint request counts/failures/durations, SQL commands/rows/seconds, folding failures, container crashes |
| `check_relationship_integrity` | yes | Key values with no match across each relationship (the (Blank) rows that silently distort totals) |
| `profile_table` | yes | Per-column blanks/distinct/min/max/longest text; blank, constant, truncated and placeholder-date detection; duplicate keys; row-count reconciliation |
| `compare_dashboards` | yes or paths | Diff two dashboards: model, DAX, RLS, Power Query steps, report pages/visuals/filters |
| `find_pbix_files` | no | Recursively scan Desktop/Documents/Downloads, OneDrive Desktop/Documents and any `POWERBI_MCP_SCAN_DIRS` folders for `.pbix`/`.pbit`; newest first; reports `truncated`/`searched`/`missing_dirs` instead of silently capping |
| `get_report_layout` | no | Pages/visuals/fields **and filters** at report/page/visual level from a `.pbix` on disk, plus hidden/drillthrough/tooltip pages, hidden and off-canvas visuals, bookmarks, slicer sync groups |

For the "yes" tools, pass whatever `list_open_files` reports as `instance`
(the file's display name, e.g. `"Roster Dashboard"`). `list_open_files` also
reports each instance's `file_path` when Power BI Desktop was launched
directly with the file (so you can feed it straight into `get_report_layout`
without hunting the path down yourself).


## Editing tools (3)

Available in the stdio server and in the Microsoft 365 Copilot build. Full rules: [EDITING.md](EDITING.md).

| Tool | Read-only? | Description |
|---|---|---|
| `review_file` | yes | Read the text of a `.pbip` `.pbir` `.pbids` `.rdl` `.tmdl` file, a PBIR `.json`, or a text part of a `.pbix`/`.pbit`. Credentials are shown as `[REDACTED]` |
| `propose_change` | no (writes only a pending record) | Validate an exact-match replacement on a copy and return a diff, affected objects and risk. Changes nothing |
| `finish_change` | no | `apply` (backup, write only the approved edit, verify, auto-restore on failure) or `rollback`. Needs a one-time code shown in the server console |

## Known limitations

- Only reads the currently-loaded data as visible to the AS engine -- it
  does not re-run Power Query refreshes (the M code it reads is the query
  *definition*, not a live re-execution of it).
- `list_power_query`'s step breakdown (`m_steps`, via `pbi_mcp/mquery.py`)
  is a structural parse of the M *source text*, not a live evaluation. It
  can show you the named steps in order and how they depend on each other,
  but not the actual data at each step the way Power Query Editor's Applied
  Steps preview does -- the mashup engine that evaluates M runs in a
  separate Power BI Desktop process with no local query API, only the
  already-loaded tabular model is reachable. For live diagnosis, pair the
  step breakdown with `diagnose_power_query`'s refresh state/error message
  (that part *is* real engine data) to at least see which table/step to
  look at, then check the actual values in Power BI Desktop's own editor --
  or record a Query Diagnostics session (see below) for real step-level data.
- `find_query_diagnostics` reads output from Power BI Desktop's built-in
  Query Diagnostics feature (Power Query Editor > Tools tab > Start/Stop
  Diagnostics) -- this is the one way to get genuine step-by-step
  evaluation data (timing, the actual query sent to each data source,
  whether query folding happened, row counts per step). This plugin cannot
  start or stop that recording itself; it's a manual, UI-only feature with
  no local API. Once you've recorded a session, `find_query_diagnostics`
  locates the resulting queries by name and reads their rows if they were
  loaded into the model (if not, it tells you to enable load on them).
  Microsoft's docs don't specify the exact query names or default load
  behavior, so this is a best-effort name search (anything containing
  "diagnostic"), not a hardcoded lookup.
- **SharePoint.Tables/SharePoint.Files folding and paging are NOT observable
  through Query Diagnostics.** Verified on a real tenant (Power BI Desktop
  2.157.x) with two Diagnose Step traces -- the `SP_Raw` navigation step and
  a full `fnGetSPList(...)` consumer chain (~6s of evaluator time): every
  `Data Source Kind`, `Data Source Query`, `Resource`, `Row Count` and
  `Content Length` value was empty; the only rows were evaluator/container
  lifecycle events. That field is only populated for some connectors, and
  SharePoint isn't one. An empty column is therefore *not* evidence that a
  filter did or didn't fold -- don't assert either from these traces. (A
  full Start/Stop Diagnostics + refresh crashed the mashup container on the
  test machine, so that path is untested.)
- Diagnose Step names its output `<Query>_<Step>_<Counters|Detailed|Aggregated>_<timestamp>`
  (Start/Stop uses `Diagnostics_<Kind>_<timestamp>`); `find_query_diagnostics`
  matches both. These queries default to Enable Load off and must be loaded
  (Enable Load + Close & Apply) before their rows can be read.
- `run_dax_query` is capped (default 1000 rows) and now reports `truncated`
  exactly (it reads one row past the cap). Internal metadata reads use a
  200,000-row cap so DMVs like VertiPaq segments aren't silently cut off.
- `list_columns`/`list_measures` with `table=` report `table_filter_applied`;
  on the raw-DMV fallback path (rows only carry a table ID) the filter can't
  be applied by name, and that is now stated instead of implied.
- `list_data_sources` only recognizes connector functions in
  `mquery.DATA_SOURCE_FUNCTIONS` -- a common list (SQL, SharePoint, Web,
  ODBC, Excel, OData, several cloud services, etc.) but not exhaustive; a
  data source via an unlisted or custom connector won't be found.
  `$SYSTEM.TMSCHEMA_DATA_SOURCES` was checked and confirmed empty for
  import-mode Power BI Desktop models, which is why this scans M text
  instead of querying a DMV.
- `analyze_model_size`'s per-table byte totals are exact (summed directly
  from the VertiPaq engine's own numbers). Its per-object breakdown is
  best-effort: it labels each internal storage structure using a name
  extracted from an internal identifier, and can't always tell a real
  column from an auto-generated internal structure (e.g. a hierarchy
  index) that happens to share a name pattern.
- `list_hierarchies` uses raw TMSCHEMA DMVs directly rather than an
  `INFO.VIEW` function (none is confirmed to exist for hierarchies on
  current engine builds), and hasn't been tested against a file that
  actually has a hierarchy defined -- it degrades gracefully (missing
  fields read as `None`) rather than assuming its column-name guesses are
  exactly right.
- `find_unused_fields` only checks whether a field appears in a visual's
  field list from the report layout. It has no visibility into fields used
  only inside another measure's DAX, tooltips, drillthrough targets, or
  Q&A -- treat its output as a lead to investigate, not a safe-to-delete
  list.
- `get_report_layout`'s filter extraction (`filters` on the report, each
  page, and each visual) pulls out the fields targeted, literal values
  compared against, and whether the condition tree contains a negation --
  it does not fully decode every filter type's condition logic (Advanced,
  TopN, and RelativeDate filters in particular carry more structure than
  this extracts), so `values`/`contains_negation` can come back empty for
  those types even when the filter is real and active.
- If Power BI Desktop is installed somewhere unusual, `discovery.py`'s
  `find_adomd_dll()` checks the classic install path first, then asks
  Windows for the Microsoft Store package location. If it still can't find
  the client DLL, this will need a small path update.

### Version 3 limits (what the new tools can and cannot prove)

- **Mashup trace logs are the only connector-level evidence** for SharePoint
  and SQL (`%USERPROFILE%\Microsoft\Power BI Desktop Store App\Traces`). They
  record request URLs/status/duration, SQL text and rows read, folding-failure
  reports and container crashes -- **not request bodies**, so SharePoint
  paging and whether a given filter folded still cannot be proven. The logs
  span every file opened in that Power BI session; failures are attributed to
  this model only when the list ID matches one of its queries.
- `analyze_folding_risk`, `analyze_sql_queries`, `analyze_sharepoint_patterns`
  and `classify_connections` are static analyses of M/SQL text (with a string-
  and comment-aware scanner, not a full M/T-SQL parser). Treat findings as
  leads with reasons, not proof.
- `profile_table` and `check_relationship_integrity` read the loaded model
  via DAX; they cannot see the source systems. Reconcile source row counts by
  passing `expected_row_count`.
- Hidden-page, drillthrough and slicer-sync detection are validated against
  all 41 sample files; **bookmarks, hidden visuals, tooltip pages and
  off-canvas visuals are implemented but no real example was available**, so
  "none detected" is not "none exist".
- Leftover Power Query diagnostics queries are excluded from findings.
- If Power BI Desktop is running with no report loaded, or a leftover engine shares a file's name, model tools now say so plainly (`model_loaded: false`, a clear error) and name lookups prefer the instance that has a model; pass the port number to pick one explicitly.
- **Portable install (v3.1):** `.mcp.json` no longer contains a machine-specific
  Python path; `launch.cmd` finds or downloads Python on each PC (see "Works on
  any Windows PC"). Tested here with an installed Python, with the downloaded
  private Python, on a damaged environment, and with two chats starting at
  once. Not tested: a PC that is genuinely offline or behind a proxy that
  blocks python.org/pypi.org, ARM64 Windows, or `AllSigned` PowerShell policy.
