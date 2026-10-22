"""Higher-level model metadata helpers built on top of engine.run_dax /
engine.run_dmv.

Prefers the modern DAX `INFO.VIEW.*` table functions, which return
human-readable metadata (real table/column names, not internal IDs). If the
engine build in use doesn't support one of those functions, each helper
falls back to the raw, decades-stable `$SYSTEM.TMSCHEMA_*` DMV and returns
it unprocessed (IDs and all) rather than guessing at an ID-join or an enum
mapping that might be wrong for that engine version. Callers get a `source`
field on the result telling them which path was used.
"""
from __future__ import annotations

import re
from typing import Any

from . import layout as layout_module
from . import mquery
from .discovery import PbiInstance
from .engine import run_dax, run_dmv


def _try_info_view(instance: PbiInstance, function: str, dmv_fallback: str) -> dict[str, Any]:
    try:
        rows = run_dax(instance, f"EVALUATE {function}")
        return {"source": f"DAX {function}", "rows": rows}
    except Exception as exc:  # noqa: BLE001
        rows = run_dmv(instance, dmv_fallback)
        return {
            "source": f"DMV fallback ({dmv_fallback}) -- {function} unsupported on this engine build: {exc}",
            "rows": rows,
        }


def list_tables(instance: PbiInstance) -> dict[str, Any]:
    return _try_info_view(instance, "INFO.VIEW.TABLES()", "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES")


def _apply_table_filter(result: dict[str, Any], table: str | None) -> dict[str, Any]:
    """Filter rows to `table` when the DAX INFO.VIEW path was used (it has a
    'Table' name column). The raw-DMV fallback only has TableID, so there the
    filter can't be applied by name -- say so explicitly instead of silently
    returning every table's rows as if they were filtered."""
    if not table:
        return result
    if result["source"].startswith("DAX"):
        result["rows"] = [r for r in result["rows"] if r.get("Table") == table]
        result["table_filter_applied"] = True
    else:
        result["table_filter_applied"] = False
    return result


def list_columns(instance: PbiInstance, table: str | None = None) -> dict[str, Any]:
    result = _try_info_view(instance, "INFO.VIEW.COLUMNS()", "SELECT * FROM $SYSTEM.TMSCHEMA_COLUMNS")
    return _apply_table_filter(result, table)


def list_measures(instance: PbiInstance, table: str | None = None) -> dict[str, Any]:
    result = _try_info_view(instance, "INFO.VIEW.MEASURES()", "SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES")
    return _apply_table_filter(result, table)


def list_relationships(instance: PbiInstance) -> dict[str, Any]:
    return _try_info_view(
        instance, "INFO.VIEW.RELATIONSHIPS()", "SELECT * FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS"
    )


def _quote_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def table_row_count(instance: PbiInstance, table: str) -> int:
    rows = run_dax(instance, f"EVALUATE ROW(\"RowCount\", COUNTROWS({_quote_table(table)}))")
    if not rows:
        return 0
    value = next(iter(rows[0].values()))
    return int(value) if value is not None else 0


def list_power_query(instance: PbiInstance, table: str | None = None, include_steps: bool = True) -> dict[str, Any]:
    """Get the Power Query (M) code behind the model: each table's load
    query (from $SYSTEM.TMSCHEMA_PARTITIONS.QueryDefinition), plus shared
    queries/functions/staging steps that don't load into a table
    (from $SYSTEM.TMSCHEMA_EXPRESSIONS -- these are the "Enable Load = Off"
    queries, e.g. a staging query merged into several visible tables).
    There's no INFO.VIEW.* function for either of these on current engine
    builds, so this always goes through the raw DMVs and does the
    partition->table name join in Python.

    When `include_steps` is True (default), each query's raw M text is also
    broken into its named steps in order (mquery.parse_m) -- see that
    module's docstring for exactly what this can and can't tell you: it's a
    structural breakdown of the M *source*, not a live evaluation, since
    the mashup engine that actually runs these steps isn't reachable from
    here. table_queries also carry the partition's last-refresh state and
    error message (if any), which *is* live engine data -- see
    diagnose_power_query() for a table-by-table version of just that.
    """
    tables_by_id = {r["ID"]: r["Name"] for r in run_dmv(instance, "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES")}

    partitions = run_dmv(
        instance,
        "SELECT [TableID], [Name], [QueryDefinition], [Mode], [State], [ErrorMessage], "
        "[RefreshedTime], [RangeStart], [RangeEnd], [Type] FROM $SYSTEM.TMSCHEMA_PARTITIONS",
    )
    table_queries = [
        {
            "table": tables_by_id.get(p["TableID"], f"table-id-{p['TableID']}"),
            "partition_name": p["Name"],
            "m_query": p["QueryDefinition"],
            # TOM PartitionSourceType: 4 = M query; other values (e.g. 2 =
            # calculated table) carry DAX in QueryDefinition, not M.
            "partition_type": p["Type"],
            "is_m": p["Type"] == 4,
            "mode": p["Mode"],
            "state": p["State"],
            "error_message": p["ErrorMessage"],
            "refreshed_time": p["RefreshedTime"],
            "incremental_refresh_range": (
                {"start": p["RangeStart"], "end": p["RangeEnd"]}
                if p.get("RangeStart") or p.get("RangeEnd")
                else None
            ),
            **(
                {"m_steps": mquery.parse_m(p["QueryDefinition"]) if p["Type"] == 4
                 else {"raw": p["QueryDefinition"], "parsed": False, "error": "not an M query (calculated table / DAX)"}}
                if include_steps else {}
            ),
        }
        for p in partitions
    ]
    if table:
        table_queries = [q for q in table_queries if q["table"] == table]

    shared_expressions = run_dmv(
        instance, "SELECT [Name], [Description], [Kind], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"
    )
    shared_queries = [
        {
            "name": e["Name"],
            "description": e["Description"],
            "kind": e["Kind"],
            "m_query": e["Expression"],
            **({"m_steps": mquery.parse_m(e["Expression"])} if include_steps else {}),
        }
        for e in shared_expressions
    ]

    return {
        "table_queries": table_queries,
        "shared_queries_and_parameters": shared_queries,
    }


def diagnose_power_query(instance: PbiInstance) -> dict[str, Any]:
    """Fast table-by-table health check for Power Query / refresh issues:
    each table's last refresh state, error message (if any), and how stale
    it is (refreshed_time vs. the query definition's modified_time -- a
    table refreshed *before* its query was last edited hasn't picked up
    that edit yet). Doesn't include the M code itself -- pair with
    list_power_query for that once you know which table to look at.

    `state` is passed through as the raw engine integer rather than decoded
    to a label: this plugin hasn't verified the PartitionState enum mapping
    against Microsoft's docs for the engine build in use, and a wrong label
    would be worse than no label. `error_message` is the reliable signal --
    non-null means that partition's last refresh failed with that message.
    """
    partitions = run_dmv(
        instance,
        "SELECT [TableID], [Name], [State], [ErrorMessage], [ModifiedTime], "
        "[RefreshedTime] FROM $SYSTEM.TMSCHEMA_PARTITIONS",
    )
    tables_by_id = {r["ID"]: r["Name"] for r in run_dmv(instance, "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES")}

    results = []
    for p in partitions:
        modified, refreshed = _parse_engine_time(p["ModifiedTime"]), _parse_engine_time(p["RefreshedTime"])
        results.append(
            {
                "table": tables_by_id.get(p["TableID"], f"table-id-{p['TableID']}"),
                "partition_name": p["Name"],
                "state": p["State"],
                "error_message": p["ErrorMessage"],
                "modified_time": p["ModifiedTime"],
                "refreshed_time": p["RefreshedTime"],
                "has_error": p["ErrorMessage"] is not None,
                # True = the query/definition was edited AFTER the last refresh,
                # so the loaded data doesn't reflect the current query. None =
                # a timestamp was missing or unparseable (don't assume).
                "refreshed_before_last_edit": (refreshed < modified) if (modified and refreshed) else None,
            }
        )

    return {
        "partitions": results,
        "error_count": sum(1 for r in results if r["has_error"]),
        "stale_count": sum(1 for r in results if r["refreshed_before_last_edit"]),
    }


def _parse_engine_time(value: Any):
    """Engine timestamps arrive as strings like '8/27/2026 8:38:48 PM'."""
    from datetime import datetime

    if not value or not isinstance(value, str):
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


# Verified live: Start/Stop Diagnostics names its output
# "Diagnostics_<Kind>_<yyyy-MM-dd_HH:mm>", while Diagnose Step names it
# "<Query>_<Step>_<Kind>_<yyyy-MM-dd_HH:mm>" (no "Diagnostics" in the name),
# with <Kind> one of Counters / Detailed / Aggregated.
_DIAGNOSTICS_NAME = re.compile(r"(?:^|_)(Counters|Detailed|Aggregated)_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$")


def _is_diagnostics_name(name: str | None) -> bool:
    if not name:
        return False
    return "diagnostic" in name.lower() or bool(_DIAGNOSTICS_NAME.search(name))


def _strip_table_prefix(column: str) -> str:
    """DAX returns 'TableName[Column]'; keep just 'Column'."""
    m = re.match(r"^.*\[(.*)\]$", column)
    return m.group(1) if m else column


def find_query_diagnostics(instance: PbiInstance, max_rows: int = 500) -> dict[str, Any]:
    """Find and read output from Power BI Desktop's built-in Query
    Diagnostics feature (Power Query Editor > Tools tab > Start/Stop
    Diagnostics), if the user has run it.

    This plugin can't trigger Query Diagnostics itself -- it's a manual,
    UI-only workflow (there's no local API to start/stop it), so this only
    helps *after* you've recorded a session yourself. Power BI does not
    load the resulting queries into the model automatically -- verified
    live that Enable Load is off by default. Names are matched by pattern:
    Start/Stop Diagnostics produces "Diagnostics_<Kind>_<timestamp>" and
    Diagnose Step produces "<Query>_<Step>_<Kind>_<timestamp>", where Kind
    is Counters / Detailed / Aggregated. So this searches both places the
    output could be:
      - loaded_tables: real tables in the model (Enable Load was on) --
        for these, actual diagnostic rows are fetched via DAX and returned
      - unloaded_queries: queries present but not loaded into the model
        (Enable Load was off) -- only the M code defining them is
        available here, not row data, since DAX can't materialize a table
        that was never loaded
    If both come back empty, no Query Diagnostics session has been
    recorded (or run against a different file than the one currently open).
    """
    tables = list_tables(instance)["rows"]
    matching_tables = [t for t in tables if _is_diagnostics_name(t.get("Name"))]

    loaded_tables = []
    for t in matching_tables:
        name = t["Name"]
        try:
            rows = run_dax(instance, f"EVALUATE {_quote_table(name)}", max_rows=max_rows)
            rows = [{_strip_table_prefix(k): v for k, v in r.items()} for r in rows]
        except Exception as exc:  # noqa: BLE001
            rows = None
            error = str(exc)
        else:
            error = None
        loaded_tables.append({"table": name, "row_count": len(rows) if rows is not None else None, "rows": rows, "error": error})

    shared = list_power_query(instance, include_steps=False)["shared_queries_and_parameters"]
    unloaded_queries = [
        {"name": s["name"], "m_query": s["m_query"]} for s in shared if _is_diagnostics_name(s["name"])
    ]

    return {
        "loaded_tables": loaded_tables,
        "unloaded_queries": unloaded_queries,
        "found_anything": bool(loaded_tables or unloaded_queries),
        "how_to_record": (
            "In Power BI Desktop: Transform Data > Power Query Editor, then on the "
            "Tools tab, select Start Diagnostics, perform the refresh/action you want "
            "traced, then select Stop Diagnostics. If the resulting Diagnostics "
            "queries show up here as unloaded_queries, right-click each one in the "
            "Queries pane, choose Enable Load, then Close & Apply so this plugin can "
            "read the actual traced rows instead of just the query definition."
        ),
    }


def list_all_dax_expressions(instance: PbiInstance) -> dict[str, Any]:
    """Every piece of DAX in the model in one place: measures, calculated
    columns, and calculated tables. (Column/table DAX is pulled from the
    same list_columns/list_tables results Claude can already fetch
    individually -- this just filters each down to entries that actually
    have an Expression, so it's a quick way to see all model DAX at once.)

    Each measure carries the engine's own `state` for that expression
    (e.g. "Valid") -- a state other than "Valid" means the engine itself
    considers that DAX broken (bad syntax, a reference to something that no
    longer exists, etc.), independent of whatever this plugin's own reading
    of the formula concludes. `broken_measures` pulls those out explicitly
    so a real error doesn't get lost in a long list of otherwise-fine DAX.
    """
    measures = list_measures(instance)["rows"]
    columns = list_columns(instance)["rows"]
    tables = list_tables(instance)["rows"]

    calculated_columns = [
        {"table": c.get("Table"), "column": c.get("Name"), "dax": c.get("Expression")}
        for c in columns
        if c.get("Expression")
    ]
    calculated_tables = [
        {"table": t.get("Name"), "dax": t.get("Expression")} for t in tables if t.get("Expression")
    ]
    measure_expressions = [
        {
            "table": m.get("Table"),
            "measure": m.get("Name"),
            "dax": m.get("Expression"),
            "state": m.get("State"),
        }
        for m in measures
    ]
    broken_measures = [m for m in measure_expressions if m["state"] not in (None, "Valid")]

    return {
        "measures": measure_expressions,
        "calculated_columns": calculated_columns,
        "calculated_tables": calculated_tables,
        "broken_measures": broken_measures,
    }


def analyze_relationships(instance: PbiInstance) -> dict[str, Any]:
    """Relationships plus derived flags worth a modeler's attention:
    many-to-many joins, bidirectional cross-filtering, inactive
    relationships, and tables with no relationships at all (isolated from
    the rest of the model).
    """
    rel_result = list_relationships(instance)
    rels = rel_result["rows"]

    flagged = []
    referenced_tables: set[str] = set()
    for r in rels:
        from_table, to_table = r.get("FromTable"), r.get("ToTable")
        from_card, to_card = r.get("FromCardinality"), r.get("ToCardinality")
        referenced_tables.update(t for t in (from_table, to_table) if t)

        flags = []
        if from_card == "Many" and to_card == "Many":
            flags.append("many_to_many")
        if r.get("CrossFilteringBehavior") == "BothDirections":
            flags.append("bidirectional")
        if r.get("IsActive") is False:
            flags.append("inactive")

        flagged.append({**r, "flags": flags})

    all_table_names = {t.get("Name") for t in list_tables(instance)["rows"] if not t.get("IsHidden") and not _is_diagnostics_name(t.get("Name"))}
    isolated_tables = sorted(all_table_names - referenced_tables)

    return {
        "source": rel_result["source"],
        "relationships": flagged,
        "many_to_many_count": sum(1 for f in flagged if "many_to_many" in f["flags"]),
        "bidirectional_count": sum(1 for f in flagged if "bidirectional" in f["flags"]),
        "inactive_count": sum(1 for f in flagged if "inactive" in f["flags"]),
        "isolated_visible_tables": isolated_tables,
    }


def list_security_roles(instance: PbiInstance) -> dict[str, Any]:
    """Row-level security (RLS) roles and their per-table filter
    expressions -- a hidden-data risk area almost by definition: a role's
    access is invisible unless you specifically go looking for it, and a
    misconfigured or missing filter can silently over- or under-expose
    data depending on who's viewing the report. For each role, also lists
    `unfiltered_tables`: tables the role has an explicit permission entry
    for but with no row filter attached, meaning that role sees every row
    of that table. That's not necessarily a bug (some tables are meant to
    be fully visible to a role), but it's exactly the kind of thing a
    security review needs surfaced rather than left implicit.
    """
    roles = run_dmv(instance, "SELECT [ID], [Name], [Description] FROM $SYSTEM.TMSCHEMA_ROLES")
    perms = run_dmv(
        instance,
        "SELECT [RoleID], [TableID], [FilterExpression], [ErrorMessage] "
        "FROM $SYSTEM.TMSCHEMA_TABLE_PERMISSIONS",
    )
    tables_by_id = {r["ID"]: r["Name"] for r in run_dmv(instance, "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES")}

    perms_by_role: dict[Any, list[dict[str, Any]]] = {}
    for p in perms:
        perms_by_role.setdefault(p["RoleID"], []).append(
            {
                "table": tables_by_id.get(p["TableID"], f"table-id-{p['TableID']}"),
                "filter_expression": p["FilterExpression"],
                "has_error": p["ErrorMessage"] is not None,
                "error_message": p["ErrorMessage"],
            }
        )

    result = []
    for r in roles:
        table_perms = perms_by_role.get(r["ID"], [])
        result.append(
            {
                "role": r["Name"],
                "description": r["Description"],
                "table_permissions": table_perms,
                "unfiltered_tables": [tp["table"] for tp in table_perms if not tp["filter_expression"]],
            }
        )

    return {
        "roles": result,
        "role_count": len(result),
        "error_count": sum(1 for r in result for tp in r["table_permissions"] if tp["has_error"]),
    }


def list_data_sources(instance: PbiInstance) -> dict[str, Any]:
    """Inventory every data-source connection the model actually makes:
    scans every table query and shared query's M code for known connector
    function calls (Sql.Database, SharePoint.Files, Web.Contents, etc.) and
    returns the distinct connections found plus which queries use each one.
    There's no reliable local DMV for this on import-mode Power BI Desktop
    models ($SYSTEM.TMSCHEMA_DATA_SOURCES is empty for these -- verified
    against a real file with several real data sources), so this reads the
    M source text instead. A connector not in mquery.DATA_SOURCE_FUNCTIONS
    won't be found; it's a known list of common connectors, not exhaustive.
    """
    pq = list_power_query(instance, include_steps=False)

    raw_matches = []
    for q in pq["table_queries"]:
        for ds in mquery.find_data_source_calls(q["m_query"] or ""):
            raw_matches.append({"scope": "table", "query": q["table"], **ds})
    for q in pq["shared_queries_and_parameters"]:
        for ds in mquery.find_data_source_calls(q["m_query"] or ""):
            raw_matches.append({"scope": "shared", "query": q["name"], **ds})

    # Group by connection identity (server/database, or URL), not the full
    # argument list -- most of these connectors take a trailing query/options
    # argument (e.g. Sql.Database's embedded [Query=...]) that varies per
    # call even when every call hits the exact same server. Keying on the
    # full tuple would wrongly count "3 queries against the same database"
    # as 3 separate connections; keying on just the leading identity args
    # correctly merges them into one connection used by several queries.
    distinct: dict[tuple, dict[str, Any]] = {}
    for m in raw_matches:
        identity = tuple(m["arguments_preview"][:2]) or (f"(expression) {m['arguments_raw']}",)
        key = (m["function"], identity)
        entry = distinct.setdefault(key, {"function": m["function"], "connection": list(identity), "used_by": []})
        entry["used_by"].append(f"{m['scope']}:{m['query']}")

    return {"connections": list(distinct.values()), "connection_count": len(distinct)}


def analyze_model_size(instance: PbiInstance, top_n: int = 25) -> dict[str, Any]:
    """VertiPaq storage engine byte totals per table, plus a best-effort
    per-column breakdown -- the "what's actually bloating my model" view
    real BI performance tuning starts from. Verified live against
    $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS (the same DMV DAX
    Studio's and Tabular Editor's VertiPaq Analyzer read).

    `tables_by_size` is exact: it's a direct sum of the engine's own
    reported allocated/used bytes per table, grouped by the verified-clean
    table name (DISCOVER's own DIMENSION_NAME column). The per-object
    breakdown is best-effort: VertiPaq creates several internal storage
    structures per column (dictionaries, attribute hierarchies, position
    indexes), and this extracts a display name from an internal identifier
    string without being able to tell which of those structures a given
    row actually is -- so an entry there is usually a real column, but can
    occasionally be internal overhead. Use the per-table totals when you
    need a number you can trust exactly; use the per-object list as a
    pointer to what to look at, not a verified column-by-column bill.
    """
    segments = run_dmv(
        instance,
        "SELECT [DIMENSION_NAME], [TABLE_ID], [ALLOCATED_SIZE], [USED_SIZE] "
        "FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS",
    )

    name_pattern = re.compile(r"\$([^$]+?)\s*\(\d+\)$")
    per_table: dict[str, dict[str, Any]] = {}
    per_object: dict[tuple, dict[str, Any]] = {}

    for s in segments:
        table = s.get("DIMENSION_NAME") or "Unknown"
        allocated = s.get("ALLOCATED_SIZE") or 0
        used = s.get("USED_SIZE") or 0

        t_entry = per_table.setdefault(table, {"table": table, "allocated_bytes": 0, "used_bytes": 0})
        t_entry["allocated_bytes"] += allocated
        t_entry["used_bytes"] += used

        match = name_pattern.search(s.get("TABLE_ID") or "")
        obj_name = match.group(1) if match else (s.get("TABLE_ID") or "unknown")
        o_key = (table, obj_name)
        o_entry = per_object.setdefault(
            o_key, {"table": table, "object": obj_name, "allocated_bytes": 0, "used_bytes": 0}
        )
        o_entry["allocated_bytes"] += allocated
        o_entry["used_bytes"] += used

    tables_sorted = sorted(per_table.values(), key=lambda r: r["allocated_bytes"], reverse=True)
    objects_sorted = sorted(per_object.values(), key=lambda r: r["allocated_bytes"], reverse=True)[:top_n]

    return {
        "total_allocated_bytes": sum(r["allocated_bytes"] for r in tables_sorted),
        "tables_by_size": tables_sorted,
        "largest_storage_objects": objects_sorted,
    }


def list_hierarchies(instance: PbiInstance) -> dict[str, Any]:
    """User-defined hierarchies (e.g. a Year > Quarter > Month date
    hierarchy, or a Region > State > City geography hierarchy) and their
    ordered levels. Uses the raw TMSCHEMA DMVs directly rather than an
    INFO.VIEW function -- unlike tables/columns/measures/relationships,
    there's no confirmed INFO.VIEW.HIERARCHIES on current engine builds,
    and this plugin hasn't been able to verify the exact column names
    against a file that actually has a hierarchy defined (none of the
    files used to build this plugin had one), so this degrades gracefully
    (missing keys read as None) rather than assuming.
    """
    hierarchies = run_dmv(instance, "SELECT * FROM $SYSTEM.TMSCHEMA_HIERARCHIES")
    levels = run_dmv(instance, "SELECT * FROM $SYSTEM.TMSCHEMA_LEVELS")
    tables_by_id = {r["ID"]: r["Name"] for r in run_dmv(instance, "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES")}
    columns_by_id = {c["ID"]: c["Name"] for c in list_columns(instance)["rows"] if "ID" in c}

    levels_by_hierarchy: dict[Any, list[dict[str, Any]]] = {}
    for lvl in levels:
        hid = lvl.get("HierarchyID")
        column_id = lvl.get("ColumnID")
        levels_by_hierarchy.setdefault(hid, []).append(
            {
                "name": lvl.get("Name"),
                "ordinal": lvl.get("Ordinal"),
                "column": columns_by_id.get(column_id, f"column-id-{column_id}"),
            }
        )

    result = []
    for h in hierarchies:
        hid = h.get("ID")
        levels_for_h = levels_by_hierarchy.get(hid, [])
        levels_for_h.sort(key=lambda lv: (lv["ordinal"] is None, lv["ordinal"]))
        result.append(
            {
                "table": tables_by_id.get(h.get("TableID"), f"table-id-{h.get('TableID')}"),
                "hierarchy": h.get("Name"),
                "is_hidden": h.get("IsHidden"),
                "levels": levels_for_h,
            }
        )

    return {"hierarchies": result, "hierarchy_count": len(result)}


_DAX_QUOTED = re.compile(r"'((?:[^']|'')+)'\s*\[([^\]]+)\]")
_DAX_PLAIN = re.compile(r"(?<![\w'\]])([A-Za-z_]\w*)\s*\[([^\]]+)\]")
_DAX_BARE = re.compile(r"(?<![\w'\]\)])\[([^\]]+)\]")


def dax_references(texts) -> tuple[set[tuple[str, str]], set[str]]:
    """Fields referenced by DAX text: (qualified {(table, name)}, bare {name}).
    Bare `[X]` is a measure, or a column of the current table, so it can only
    be matched by name (conservatively: any field called X counts as used)."""
    qualified: set[tuple[str, str]] = set()
    bare: set[str] = set()
    for t in texts:
        if not t:
            continue
        s = re.sub(r'"(?:[^"]|"")*"', '""', t)
        s = re.sub(r"//[^\n]*|--[^\n]*|/\*.*?\*/", " ", s, flags=re.S)
        for m in _DAX_QUOTED.finditer(s):
            qualified.add((m.group(1).replace("''", "'"), m.group(2)))
        for m in _DAX_PLAIN.finditer(s):
            qualified.add((m.group(1), m.group(2)))
        rest = _DAX_PLAIN.sub(" ", _DAX_QUOTED.sub(" ", s))
        for m in _DAX_BARE.finditer(rest):
            bare.add(m.group(1))
    return qualified, bare


def find_unused_fields(instance: PbiInstance, pbix_path: str) -> dict[str, Any]:
    """Which visible columns/measures does the *report* never show -- and, for
    those, which are still needed by the model itself?

    "Used by the report" counts every place a field can appear on a page, not
    just the visual wells: conditional formatting, titles, tooltips, sort
    orders and filters at report/page/visual level (entity aliases resolved).
    A field the report doesn't show is then split:
      - unused_*: nothing references it anywhere -- the real cleanup leads
      - used_only_by_model_*: hidden from the report but needed by the model,
        with the reason(s): DAX (measure / calculated column / calculated
        table), relationship key, sort-by column, hierarchy level, RLS filter
    `approx_bytes` (best-effort, from the VertiPaq storage breakdown) ranks
    unused columns by how much memory removing them could save.
    """
    columns = list_columns(instance)["rows"]
    measures = list_measures(instance)["rows"]
    tables = list_tables(instance)["rows"]
    layout_result = layout_module.parse_layout(pbix_path)

    used_refs: set[str] = set()
    legacy_fields: set[str] = set()

    def add_filters(fl):
        for f in fl or []:
            used_refs.update(f.get("field_refs") or [])

    add_filters(layout_result.get("report_filters"))
    for page in layout_result["pages"]:
        add_filters(page.get("filters"))
        for v in page["visuals"]:
            used_refs.update(v.get("field_refs") or [])
            legacy_fields.update(v.get("fields") or [])
            add_filters(v.get("filters"))

    def in_report(table, name) -> bool:
        if f"{table}.{name}" in used_refs:
            return True
        pat = re.compile(r"(?:^|\()" + re.escape(f"{table}.{name}") + r"(?:\)|\.|$)")
        return any(pat.search(f) for f in legacy_fields)

    # ---- what the model itself references -------------------------------
    dax_texts = [m.get("Expression") for m in measures] + [c.get("Expression") for c in columns] + [t.get("Expression") for t in tables]
    dax_q, dax_b = dax_references(dax_texts)
    roles = list_security_roles(instance)["roles"]
    rls_q, rls_b = dax_references([tp["filter_expression"] for r in roles for tp in r["table_permissions"]])
    rel_keys: set[tuple[str, str]] = set()
    for r in list_relationships(instance)["rows"]:
        rel_keys.add((r.get("FromTable"), r.get("FromColumn")))
        rel_keys.add((r.get("ToTable"), r.get("ToColumn")))
    sort_keys = {(c.get("Table"), c.get("SortByColumn")) for c in columns if isinstance(c.get("SortByColumn"), str)}
    hier_keys: set[tuple[str, str]] = set()
    try:
        for h in list_hierarchies(instance)["hierarchies"]:
            for lv in h["levels"]:
                hier_keys.add((h["table"], lv["column"]))
    except Exception:  # noqa: BLE001
        pass

    def reasons(table, name) -> list[str]:
        out = []
        if (table, name) in dax_q or name in dax_b:
            out.append("dax")
        if (table, name) in rls_q or name in rls_b:
            out.append("rls_filter")
        if (table, name) in rel_keys:
            out.append("relationship_key")
        if (table, name) in sort_keys:
            out.append("sort_by_column")
        if (table, name) in hier_keys:
            out.append("hierarchy_level")
        return out

    try:
        objs = analyze_model_size(instance, top_n=1_000_000)["largest_storage_objects"]
        size = {(o["table"], o["object"]): o["allocated_bytes"] for o in objs}
    except Exception:  # noqa: BLE001
        size = {}

    unused_c, model_c, unused_m, model_m = [], [], [], []
    for c in columns:
        t, n = c.get("Table"), c.get("Name")
        if c.get("IsHidden") or not t or not n or str(n).startswith("RowNumber-") or in_report(t, n):
            continue
        rs = reasons(t, n)
        row = {"table": t, "column": n, "approx_bytes": size.get((t, n))}
        (model_c if rs else unused_c).append({**row, **({"reasons": rs} if rs else {})})
    for m in measures:
        t, n = m.get("Table"), m.get("Name")
        if m.get("IsHidden") or not t or not n or in_report(t, n):
            continue
        rs = reasons(t, n)
        (model_m if rs else unused_m).append({"table": t, "measure": n, **({"reasons": rs} if rs else {})})
    unused_c.sort(key=lambda r: -(r["approx_bytes"] or 0))

    return {
        "unused_visible_columns": unused_c,
        "unused_visible_measures": unused_m,
        "used_only_by_model_columns": model_c,
        "used_only_by_model_measures": model_m,
        "counts": {"unused_columns": len(unused_c), "unused_measures": len(unused_m),
                   "model_only_columns": len(model_c), "model_only_measures": len(model_m)},
        "note": (
            "Report usage covers visual wells, conditional formatting, titles, tooltips, sort orders and "
            "filters at all levels. It cannot see Q&A, external report connections, or other reports built on "
            "this model -- treat `unused_*` as leads, not a safe-to-delete list. `approx_bytes` is best-effort "
            "(see analyze_model_size). Hidden fields are excluded as intentional."
        ),
    }


def model_summary(instance: PbiInstance) -> dict[str, Any]:
    tables = list_tables(instance)
    measures = list_measures(instance)
    relationships = list_relationships(instance)
    return {
        "instance": instance.display_name,
        "port": instance.port,
        "table_count": len(tables["rows"]),
        "measure_count": len(measures["rows"]),
        "relationship_count": len(relationships["rows"]),
        "tables": tables,
        "measures": measures,
        "relationships": relationships,
    }
