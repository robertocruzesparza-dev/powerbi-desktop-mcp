"""MCP server exposing Power BI Desktop's live data model and report layout
to Claude.

Two independent capabilities:
  1. Live model (tables, columns, measures, relationships, arbitrary DAX) --
     requires Power BI Desktop to be open with a .pbix file loaded.
  2. Report layout (pages, visuals, fields used) -- read straight from the
     .pbix zip, works whether or not Power BI Desktop is running.
"""
from __future__ import annotations

from typing import Any, Literal

from mcp.types import ToolAnnotations

from . import audit, compare, editing, insight, layout, metadata, quality
from .engine import resolve_instance, run_dax_ex
from .mcp_base import PbiMCPServer

mcp = PbiMCPServer("powerbi-desktop")


@mcp.tool()
def list_open_files() -> list[dict[str, Any]]:
    """List every Power BI Desktop file currently open on this machine,
    with the identifier ('instance') to pass to the other model tools.
    Each entry has `model_loaded`: false means Power BI Desktop is running but
    no report is loaded in it (closed, still opening, or a dialog is open) and
    every model tool will fail until the file is loaded -- ask the user to
    open it. Returns an empty list if Power BI Desktop isn't running.
    """
    from .engine import list_instances, model_loaded

    out = []
    for inst in list_instances():
        loaded = model_loaded(inst.port)
        entry = {"instance": inst.display_name, "port": inst.port, "file_path": inst.file_path, "model_loaded": loaded}
        if loaded is False:
            entry["note"] = "Power BI Desktop is running but no model is loaded in it; open the file and wait for it to finish loading."
        out.append(entry)
    return out


@mcp.tool()
def get_model_summary(instance: str) -> dict[str, Any]:
    """Get an overview of an open Power BI file's data model: table count,
    measure count, relationship count, plus the full table/measure/
    relationship lists. `instance` is the file's display name (or port)
    returned by list_open_files.
    """
    return metadata.model_summary(resolve_instance(instance))


@mcp.tool()
def list_tables(instance: str) -> dict[str, Any]:
    """List every table in the data model, with visibility/description
    metadata where available."""
    return metadata.list_tables(resolve_instance(instance))


@mcp.tool()
def list_columns(instance: str, table: str | None = None) -> dict[str, Any]:
    """List columns across the model, or for one table if `table` is given.
    Includes data type, hidden flag, and calculated-column DAX expression
    where applicable."""
    return metadata.list_columns(resolve_instance(instance), table)


@mcp.tool()
def list_measures(instance: str, table: str | None = None) -> dict[str, Any]:
    """List DAX measures across the model, or for one table if `table` is
    given. Includes each measure's DAX expression and format string."""
    return metadata.list_measures(resolve_instance(instance), table)


@mcp.tool()
def list_relationships(instance: str) -> dict[str, Any]:
    """List relationships between tables: from/to table and column,
    cardinality, active flag, cross-filter direction."""
    return metadata.list_relationships(resolve_instance(instance))


@mcp.tool()
def get_table_row_count(instance: str, table: str) -> int:
    """Get the number of rows currently loaded in a table."""
    return metadata.table_row_count(resolve_instance(instance), table)


@mcp.tool()
def list_power_query(instance: str, table: str | None = None, include_steps: bool = True) -> dict[str, Any]:
    """Get the Power Query (M) code behind the model: each loaded table's
    query steps (the let/in M code, including its transformation step
    names), plus shared queries/functions/staging steps that feed other
    queries but don't load into a visible table themselves (Enable Load =
    Off). Pass `table` to filter table_queries to one table; shared queries
    aren't table-scoped so they're always returned in full.

    With `include_steps` (default True), each query also comes broken into
    its named steps in source order, each with the sub-step names it
    references -- e.g. step "Changed Type" references step "Source". This
    is a structural breakdown of the M *source text*, not a live
    evaluation: there's no local API into the mashup engine that actually
    executes these steps, only into the already-loaded tabular model, so
    this can't show you the data at each step the way Power Query Editor's
    step preview does -- it can only show you the steps and how they chain
    together, which is usually enough to spot which step to look at. Each
    table_queries entry also includes the partition's live refresh state
    and error_message; use diagnose_power_query for just that, across all
    tables, without the M text.
    """
    return metadata.list_power_query(resolve_instance(instance), table, include_steps)


@mcp.tool()
def find_query_diagnostics(instance: str, max_rows: int = 500) -> dict[str, Any]:
    """Find and read output from Power BI Desktop's built-in Query
    Diagnostics feature, if you've recorded a session. This plugin can't
    start/stop Query Diagnostics itself -- it's a manual step in the Power
    Query Editor's Tools tab (Start Diagnostics -> do the refresh you want
    traced -> Stop Diagnostics) with no local API to trigger it -- but once
    you've recorded one, this finds the resulting "Diagnostics" queries by
    name and, for any that were loaded into the model, returns the actual
    traced rows (step-by-step timing, data source queries, row counts,
    whether query folding happened, and so on -- the real intermediate
    evaluation data list_power_query's step breakdown can't provide). If
    the diagnostics queries exist but weren't loaded into the model, this
    tells you that and how to fix it rather than returning nothing.
    """
    return metadata.find_query_diagnostics(resolve_instance(instance), max_rows)


@mcp.tool()
def diagnose_power_query(instance: str) -> dict[str, Any]:
    """Fast health check across every table's Power Query load: which
    tables errored on last refresh (with the actual error message), and
    whether a table was refreshed before or after its query was last
    edited (a stale refresh). This is the live, authoritative signal for
    "is something broken" -- check this first, then use list_power_query
    to read the M code (with its step breakdown) for whichever table shows
    a problem.
    """
    return metadata.diagnose_power_query(resolve_instance(instance))


@mcp.tool()
def list_all_dax_expressions(instance: str) -> dict[str, Any]:
    """Get every piece of DAX in the model in one place: measures,
    calculated columns, and calculated tables, each with its full
    expression. Useful for an overall DAX audit/review rather than pulling
    measures and columns separately."""
    return metadata.list_all_dax_expressions(resolve_instance(instance))


@mcp.tool()
def analyze_relationships(instance: str) -> dict[str, Any]:
    """List relationships with modeling-analysis flags attached: which are
    many-to-many, which cross-filter bidirectionally, which are inactive,
    and which visible tables have no relationships at all (isolated from
    the rest of the model). Use this over list_relationships when the goal
    is spotting model design issues rather than just reading the raw list.
    """
    return metadata.analyze_relationships(resolve_instance(instance))


@mcp.tool()
def list_security_roles(instance: str) -> dict[str, Any]:
    """List row-level security (RLS) roles and each one's per-table filter
    expression. Flags, per role, which tables it can access with no row
    filter at all (`unfiltered_tables` -- sees every row of that table).
    RLS is invisible unless specifically checked, so this is exactly the
    kind of thing a "who can see what" or security-focused review needs.
    """
    return metadata.list_security_roles(resolve_instance(instance))


@mcp.tool()
def list_data_sources(instance: str) -> dict[str, Any]:
    """Inventory every data source the model actually connects to (SQL
    servers, SharePoint sites, web APIs, files, etc.), by scanning every
    table and shared query's M code for known connector function calls.
    Returns each distinct connection plus which queries use it. Useful for
    "what does this dashboard actually talk to" -- spotting a dev/test
    server left in a query bound for production, an unexpected data
    source, or confirming everything points where it should.
    """
    return metadata.list_data_sources(resolve_instance(instance))


@mcp.tool()
def analyze_model_size(instance: str, top_n: int = 25) -> dict[str, Any]:
    """Get VertiPaq storage engine byte totals per table (exact) and the
    largest individual storage objects/columns (best-effort labeling) --
    the same underlying data DAX Studio's and Tabular Editor's VertiPaq
    Analyzer use to show what's bloating a model's memory footprint. Use
    this for efficiency review: a surprisingly large table or column here
    is a concrete lead (unused high-cardinality column, a text column that
    should be split or removed, importing more history than needed, etc).
    """
    return metadata.analyze_model_size(resolve_instance(instance), top_n)


@mcp.tool()
def list_hierarchies(instance: str) -> dict[str, Any]:
    """List user-defined hierarchies (e.g. Year > Quarter > Month, or
    Region > State > City) and their ordered levels, for a complete picture
    of the model's structure beyond flat tables/columns."""
    return metadata.list_hierarchies(resolve_instance(instance))


@mcp.tool()
def find_unused_fields(instance: str, pbix_path: str) -> dict[str, Any]:
    """Find visible columns and measures that no visual on any report page
    actually uses (cross-references the live model against the offline
    report layout). Get `pbix_path` from list_open_files()'s `file_path`,
    or find_pbix_files(). A lead worth investigating for model cleanup, not
    a guaranteed-safe-to-delete list -- see the tool's `note` field for
    what it can't see (DAX-only references, tooltips, drillthrough, Q&A).
    """
    return metadata.find_unused_fields(resolve_instance(instance), pbix_path)


@mcp.tool()
def audit_dashboard(instance: str, pbix_path: str | None = None, include_data_quality: bool = True, include_traces: bool = True) -> dict[str, Any]:
    """START HERE for a full review. Runs every check (refresh health, DAX
    validity, relationships, row-level security, connections, embedded SQL,
    query folding, SharePoint patterns, dependencies, report filters/hidden
    pages, unused fields, model size, relationship data integrity, and
    Power BI's own runtime logs) and returns ONE ranked list of findings,
    each tagged with the tool to call for full detail. A failing check is
    reported in `checks_failed` and never hides the rest; `not_checked`
    lists what this audit cannot see. `pbix_path` defaults to the open
    file's path.
    """
    return audit.audit_dashboard(resolve_instance(instance), pbix_path, include_data_quality, include_traces)


@mcp.tool()
def classify_connections(instance: str) -> dict[str, Any]:
    """Every data source the model reads, classified (on-prem SQL Server,
    SharePoint site, personal OneDrive, web, local/UNC file, ...) with the
    problems that matter for SQL / SharePoint / OneDrive dashboards: gateway
    needed for service refresh, localhost or OneDrive-synced local paths
    (only work on this PC), http, personal-OneDrive dependence, dev/test
    names, the same target spelled several ways (separate credentials), and
    parameters resolved to their values."""
    return insight.classify_connections(resolve_instance(instance))


@mcp.tool()
def analyze_sql_queries(instance: str) -> dict[str, Any]:
    """Review every hand-written SQL statement embedded in the M code
    ([Query=...], Value.NativeQuery): writes/DROP against real tables in a
    read query, TOP N caps that silently drop rows, SELECT *, hard-coded
    years / year-named tables, CAST to varchar without length (truncates at
    30), NOLOCK, UNION vs UNION ALL, ORDER BY, temp-table batches, cross-
    database reads, and SQL built by concatenation. Also lists navigation-
    style SQL queries (which can fold)."""
    return insight.analyze_sql_queries(resolve_instance(instance))


@mcp.tool()
def analyze_folding_risk(instance: str, include_steps: bool = False) -> dict[str, Any]:
    """Static estimate of query folding per query: which steps normally push
    down to SQL, the first step that stops folding, row filters that run
    locally AFTER folding ended (the source sent every row first), Table.Buffer,
    and merges across different sources (joined locally). SharePoint/file
    sources never fold. It is an estimate from M text, not proof: confirm
    with 'View Native Query', and use read_mashup_traces for the engine's
    own folding-failure reports."""
    return insight.analyze_folding_risk(resolve_instance(instance), include_steps)


@mcp.tool()
def analyze_sharepoint_patterns(instance: str, include_row_counts: bool = True) -> dict[str, Any]:
    """SharePoint loading review: which lists/files each query pulls (by
    ID), lists fetched by several queries, SharePoint.Files enumerating the
    whole site then filtering by name, queries that never reduce columns or
    rows, system columns deleted by name, errors swallowed by
    `try ... otherwise null/false` (refresh succeeds with missing data),
    the shared navigation table's blast radius, and hard-coded list IDs."""
    return insight.analyze_sharepoint_patterns(resolve_instance(instance), include_row_counts)


@mcp.tool()
def check_refresh_dependencies(instance: str) -> dict[str, Any]:
    """The query dependency graph: what feeds what, chain depth, which
    loaded tables fail if each source (SQL server / SharePoint site) is
    unreachable, high fan-out staging queries, orphan queries nothing uses,
    parameters and their consumers, circular references."""
    return insight.check_refresh_dependencies(resolve_instance(instance))


@mcp.tool()
def read_mashup_traces(instance: str | None = None, since_minutes: int = 1440) -> dict[str, Any]:
    """Read Power BI Desktop's own mashup-engine trace logs
    (%USERPROFILE%\\Microsoft\\Power BI Desktop Store App\\Traces): the
    ONLY source of connector-level runtime evidence. Per SharePoint list:
    request counts, failures with HTTP status (e.g. 500/400), durations,
    the navigation table re-fetched per evaluation; per SQL command: the
    text sent, seconds, rows read; the engine's own folding-failure
    reports; container crashes. Pass `instance` to match SharePoint list IDs
    to this model's queries (the logs cover every file opened in the
    window). Request bodies aren't logged, so paging can't be proven."""
    return insight.read_mashup_traces(resolve_instance(instance) if instance else None, since_minutes)


@mcp.tool()
def check_relationship_integrity(instance: str, max_relationships: int = 60) -> dict[str, Any]:
    """For every relationship, count key values with no match on the other
    side. On many-to-one lookups these are rows that show under (Blank) and
    vanish from cross-filtered measures -- totals stop matching with no
    error. One-to-one/many-to-many are flagged only when unmatched from both
    sides (wrong columns or a data-type/format mismatch)."""
    return quality.check_relationship_integrity(resolve_instance(instance), max_relationships)


@mcp.tool()
def profile_table(instance: str, table: str, columns: list[str] | None = None, key_columns: list[str] | None = None,
                  expected_row_count: int | None = None, max_columns: int = 80) -> dict[str, Any]:
    """Data-quality profile of one loaded table: per column blanks, distinct
    count, min/max, longest text; flags entirely-blank / constant columns,
    text at a truncation limit (30/255...), placeholder dates (1900/9999).
    `key_columns` checks duplicates and blank keys; `expected_row_count`
    (e.g. COUNT(*) you ran on the source) flags a mismatch."""
    return quality.profile_table(resolve_instance(instance), table, columns, key_columns, expected_row_count, max_columns)


@mcp.tool()
def compare_dashboards(left: str, right: str) -> dict[str, Any]:
    """Diff two dashboards. Each of `left`/`right` is an open file's name
    (from list_open_files) or a .pbix path. With both open: tables, columns,
    measures (DAX), relationships, RLS roles and Power Query (step-level).
    Any two .pbix paths: report pages, visuals and filters. Useful for
    backup vs current, or before/after a change."""
    return compare.compare_dashboards(left, right)


@mcp.tool()
def run_dax_query(instance: str, dax_query: str, max_rows: int = 1000) -> dict[str, Any]:
    """Run an arbitrary DAX query (an EVALUATE statement, optionally with
    DEFINE MEASURE/VAR blocks above it) against an open Power BI file. Use
    this for anything the higher-level tools (list_tables, list_measures,
    etc.) don't cover -- ad hoc aggregations, filtering, top-N, DISTINCT
    lists, testing a candidate measure definition, and so on.

    Returns {"rows": [...], "row_count": n, "truncated": bool}. Results are
    capped at `max_rows`; when `truncated` is true there were MORE rows than
    were returned, so never draw a "this is everything" conclusion from a
    truncated result -- aggregate in DAX (or raise max_rows) instead.
    """
    rows, truncated = run_dax_ex(resolve_instance(instance), dax_query, max_rows)
    return {"rows": rows, "row_count": len(rows), "truncated": truncated}


@mcp.tool()
def find_pbix_files(search_dirs: list[str] | None = None, max_results: int = 300) -> dict[str, Any]:
    """Scan folders recursively for .pbix / .pbit files on disk (no Power BI
    Desktop needed). Defaults: Desktop, Documents, Downloads and OneDrive
    Desktop/Documents (add more folders with POWERBI_MCP_SCAN_DIRS). Pass `search_dirs` to scan specific
    folders instead. Returns {files (newest first), count, total_found,
    truncated, searched, missing_dirs}; if `truncated` is true there are more
    files than `max_results` -- raise it or narrow `search_dirs`."""
    return layout.scan_pbix_files(search_dirs, max_results)


@mcp.tool()
def get_report_layout(pbix_path: str) -> dict[str, Any]:
    """Parse a .pbix file's report layout offline (no need for Power BI
    Desktop to be running): pages, and per page the visuals with their
    type, title, and the fields each visual uses. Does not include the
    data model itself -- use the live-model tools for tables/measures/DAX.
    """
    return layout.parse_layout(pbix_path)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
def review_file(path: str, member: str = "", offset: int = 0, max_chars: int = 6000) -> dict[str, Any]:
    """Read one Power BI file's TEXT without changing anything: .pbip .pbir .pbids .rdl .tmdl, a PBIR .json
    inside a *.Report folder, or a part of a .pbix/.pbit. For a .pbix/.pbit call it WITHOUT `member` to list
    the parts, then with `member` (for example Report/Layout) to read one; the compressed model (DataModel)
    and Power Query container (DataMashup) are never read. Passwords and tokens appear as [REDACTED]."""
    return editing.review(path, member, max(0, offset), max_chars)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def propose_change(path: str, old_text: str, new_text: str, reason: str, member: str = "", expected_count: int = 1) -> dict[str, Any]:
    """STEP 1 of any edit. Proposes replacing exact `old_text` with `new_text` in a file (.pbix .pbit .pbip
    .pbir .pbids .rdl .tmdl or PBIR .json). Changes NOTHING: validates the result and returns a diff, the
    affected objects and the risk. For .pbix/.pbit pass `member`. `old_text` must match exactly
    `expected_count` places. Show the user the diff verbatim and wait for explicit approval.
    Editing works only on COPIES inside the folder named by the PBI_MCP_EDIT_ROOT environment variable
    (off when unset); originals elsewhere are refused."""
    return editing.propose(path, old_text, new_text, reason, member, max(1, expected_count))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def finish_change(change_id: str, action: Literal["apply", "rollback"], approval_code: str = "") -> dict[str, Any]:
    """STEP 2 (apply) or UNDO (rollback) for a change from propose_change. Call ONLY after the user has
    explicitly approved THAT change and read you the 6-digit approval code shown in the server's console
    window; never guess a code or infer approval. With an empty `approval_code` it issues a fresh code and
    applies nothing. apply backs up the whole file, applies only the approved edit, re-verifies, and restores
    the original if verification fails. rollback restores the backup (needs its own code).
    Works only on COPIES inside the folder named by PBI_MCP_EDIT_ROOT (off when unset)."""
    return editing.finish(change_id, action, approval_code)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
