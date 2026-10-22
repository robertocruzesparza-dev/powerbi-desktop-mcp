"""Consolidated, read-only MCP surface for Microsoft 365 Copilot.

Microsoft says a plugin's answer quality degrades past about 10 tools, and
server.py exposes 30. This module re-exposes the same analysis code as 9 tools
by grouping related functions behind an `aspect` / `check` argument, so nothing
is lost and Copilot sees a small tool list. The Claude plugin keeps using
server.py unchanged.

Differences from server.py that exist only because Copilot is a remote client:
  * every tool is marked readOnlyHint, so Copilot does not ask the user to
    confirm each call -- which means every input has to be safe to run
    unattended (DAX must be a read-only query, file arguments must be .pbix);
  * arguments are flat primitives (no unions/lists), which Copilot handles best;
  * results are returned as compact JSON text and trimmed to a size budget,
    because Copilot truncates large tool output.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import editing
from . import server as base

mcp = MCPServer(
    "powerbi-desktop",
    instructions=(
        "Analysis of Power BI Desktop files open on the host PC, plus approved, reversible edits to COPIES of "
        "Power BI files. Call find_dashboards first; use audit_dashboard for a full review; drill in with "
        "inspect_model, inspect_power_query and check_data_quality. Edits: review_file, then propose_change, "
        "then finish_change only after the user approves and reads the approval code."
    ),
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


_WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _write_tool():
    """Register a tool that can change a file. NOT readOnlyHint, so Microsoft 365 Copilot asks the user
    to confirm each call. propose_change itself writes nothing to the dashboard (it only records a
    pending proposal), but is registered the same way so the user sees every step."""
    return mcp.tool(annotations=_WRITES, structured_output=False)


def _tool():
    """Register a read-only tool. structured_output=False: results are already compact JSON
    text, and a structured copy would send Copilot every result twice."""
    return mcp.tool(annotations=_READ_ONLY, structured_output=False)

# Copilot truncates big tool output; stay well under it and say so when we trim.
MAX_RESPONSE_CHARS = int(os.environ.get("PBI_MCP_MAX_RESPONSE_CHARS", "24000"))
_MAX_STRING_CHARS = 2000
# run_dax_query is the one tool that returns raw model rows; keep it small.
MAX_DAX_ROWS = int(os.environ.get("PBI_MCP_MAX_DAX_ROWS", "500"))

_PBIX_SUFFIXES = (".pbix", ".pbit")


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------

def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def _cap_strings(node: Any) -> None:
    """Shorten very long strings in place (raw M code, SQL text, ...)."""
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in list(items):
        if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
            node[key] = f"{value[:_MAX_STRING_CHARS]}...[+{len(value) - _MAX_STRING_CHARS} chars]"
        elif isinstance(value, (dict, list)):
            _cap_strings(value)


def _largest_list(node: Any, path: str = "$") -> tuple[int, list[Any] | None, str]:
    """(serialized size, the list, its path) of the biggest list with 2+ items."""
    best: tuple[int, list[Any] | None, str] = (0, None, "")
    if isinstance(node, list):
        if len(node) >= 2:
            best = (len(_dump(node)), node, path)
        children = list(enumerate(node))
    elif isinstance(node, dict):
        children = list(node.items())
    else:
        return best
    for key, child in children:
        if isinstance(child, (dict, list)):
            candidate = _largest_list(child, f"{path}[{key}]" if isinstance(node, list) else f"{path}.{key}")
            if candidate[0] > best[0]:
                best = candidate
    return best


def shrink(payload: Any, budget: int = MAX_RESPONSE_CHARS) -> str:
    """Serialize `payload` compactly, cutting the largest lists (then long
    strings) until it fits `budget`. Anything cut is listed under `_truncated`
    so the model knows the data is partial and how to narrow the request."""
    if not isinstance(payload, dict):
        payload = {"result": payload}
    text = _dump(payload)
    if len(text) <= budget:
        return text

    data = json.loads(text)  # plain JSON types only, and a private copy we may edit
    _cap_strings(data)
    omitted: list[dict[str, Any]] = []
    for _ in range(200):
        text = _dump({**data, "_truncated": {"omitted": omitted}}) if omitted else _dump(data)
        if len(text) <= budget:
            break
        size, target, path = _largest_list(data)
        if target is None:
            break
        keep = max(1, len(target) // 2)
        omitted.append({"path": path, "kept": keep, "total": len(target)})
        del target[keep:]
    if omitted:
        data["_truncated"] = {
            "omitted": omitted,
            "message": (
                "Result was cut to fit Copilot's size limit, so it is PARTIAL. Do not treat it as "
                "complete: narrow the request (pass table=..., a smaller aspect, or aggregate with "
                "run_dax_query) and say the data is partial."
            ),
        }
        text = _dump(data)
    return text


# --------------------------------------------------------------------------
# Input guards (these tools run without a per-call confirmation prompt)
# --------------------------------------------------------------------------

_DAX_COMMENTS = re.compile(r"--[^\n]*|//[^\n]*|/\*.*?\*/", re.S)


def require_read_only_dax(query: str) -> str:
    """Allow only DAX queries (EVALUATE, optionally after DEFINE).

    The engine hands the text straight to ADOMD, which also accepts DMV
    SELECTs, MDX and TMSL/XMLA scripts. Copilot can be steered by content it
    reads, so anything that is not a plain DAX query is refused here.
    """
    cleaned = _DAX_COMMENTS.sub(" ", query or "").strip()
    first = re.match(r"[A-Za-z]+", cleaned)
    if not first or first.group(0).upper() not in ("EVALUATE", "DEFINE"):
        raise ValueError("Only read-only DAX queries are allowed: the query must start with EVALUATE (or DEFINE ... EVALUATE).")
    return query


def require_pbix(path: str, what: str = "pbix_path") -> str:
    if not path or Path(path).suffix.lower() not in _PBIX_SUFFIXES:
        raise ValueError(f"{what} must be the path of a .pbix or .pbit file (got {path!r}). Get it from list_open_files or find_pbix_files.")
    return path


def _looks_like_path(value: str) -> bool:
    return "\\" in value or "/" in value or Path(value).suffix.lower() in _PBIX_SUFFIXES


def _split(value: str, sep: str = ",") -> list[str] | None:
    parts = [p.strip() for p in (value or "").split(sep) if p.strip()]
    return parts or None


def _search_dirs(value: str) -> list[str] | None:
    """`;`-separated folders, each of which must sit under the user's profile."""
    dirs = _split(value, ";")
    if not dirs:
        return None
    home = Path.home().resolve()
    for d in dirs:
        resolved = Path(d).expanduser().resolve()
        if resolved != home and home not in resolved.parents:
            raise ValueError(f"search_dirs must be inside {home}; {d!r} is not.")
    return dirs


def _need(value: str | int | None, arg: str, aspect: str) -> None:
    if value in ("", None):
        raise ValueError(f"aspect/check {aspect!r} needs the `{arg}` argument.")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@_tool()
def find_dashboards(search_dirs: str = "", max_results: int = 100, scan_disk: bool = False) -> str:
    """ALWAYS call this first. Returns `open_files`: the Power BI Desktop files open on the host PC, with
    the `instance` name every live tool needs, each `file_path`, and `model_loaded` (false means Desktop is
    running with no report loaded -- ask the user to open the file and wait; empty means Desktop isn't
    running). Also returns `edit_folder_files`: the copies in the edit folder that can be reviewed and
    edited (.pbix .pbit .pbip .pbir .pbids .rdl .tmdl .json), or a note that editing is off.
    Set scan_disk=true to also search the disk for .pbix/.pbit files (Desktop, Documents, Downloads,
    team OneDrive; `search_dirs` is an optional ';'-separated list of folders inside the user's profile)."""
    result: dict[str, Any] = {"open_files": base.list_open_files()}
    try:
        result["edit_folder_files"] = editing.find_editable_files(max(1, min(max_results, 300)))
    except ValueError as exc:
        result["edit_folder_files"] = {"editing": "off", "reason": str(exc)}
    if scan_disk:
        result["disk_files"] = base.find_pbix_files(_search_dirs(search_dirs), max(1, min(max_results, 300)))
    return shrink(result)


@_tool()
def audit_dashboard(instance: str, pbix_path: str = "", include_data_quality: bool = True, include_traces: bool = True) -> str:
    """START HERE for a full review of an open dashboard. Runs ~15 checks (refresh health, DAX
    validity, relationships, row-level security, connections, embedded SQL, query folding, SharePoint
    patterns, dependencies, report filters/hidden pages, unused fields, model size, relationship data
    integrity, runtime logs) and returns ONE ranked findings list; each finding names the tool/aspect
    for detail. Report `checks_failed` and ALWAYS relay `not_checked`. `pbix_path` defaults to the open
    file's path."""
    path = require_pbix(pbix_path) if pbix_path else None
    return shrink(base.audit_dashboard(instance, path, include_data_quality, include_traces))


@_tool()
def inspect_model(
    instance: str,
    aspect: Literal[
        "summary", "tables", "columns", "measures", "relationships", "dax",
        "hierarchies", "security_roles", "data_sources", "model_size", "row_count",
    ],
    table: str = "",
    top_n: int = 25,
) -> str:
    """Read the data model of an open file. `aspect`: summary (counts + lists), tables, columns,
    measures (DAX + format), relationships (with many-to-many / bidirectional / inactive / isolated-table
    flags), dax (every measure, calculated column and calculated table; check `broken_measures` first),
    hierarchies, security_roles (RLS filters and tables with no filter), data_sources, model_size
    (VertiPaq bytes per table, `top_n` largest objects), row_count (needs `table`). `table` narrows
    columns and measures to one table -- do that on big models to avoid a truncated answer."""
    tbl = table or None
    if aspect == "summary":
        # Counts and table names only: the full measure/relationship lists routinely blow
        # Copilot's size limit, and aspect=measures / relationships return them properly.
        full = base.get_model_summary(instance)
        result = {k: v for k, v in full.items() if k not in ("measures", "relationships")}
        result["note"] = "Measure and relationship lists omitted here; use aspect=measures or aspect=relationships."
    elif aspect == "tables":
        result = base.list_tables(instance)
    elif aspect == "columns":
        result = base.list_columns(instance, tbl)
    elif aspect == "measures":
        result = base.list_measures(instance, tbl)
    elif aspect == "relationships":
        result = base.analyze_relationships(instance)
    elif aspect == "dax":
        result = base.list_all_dax_expressions(instance)
    elif aspect == "hierarchies":
        result = base.list_hierarchies(instance)
    elif aspect == "security_roles":
        result = base.list_security_roles(instance)
    elif aspect == "data_sources":
        result = base.list_data_sources(instance)
    elif aspect == "model_size":
        result = base.analyze_model_size(instance, max(1, min(top_n, 100)))
    else:  # row_count
        _need(table, "table", aspect)
        result = {"table": table, "row_count": base.get_table_row_count(instance, table)}
    return shrink(result)


@_tool()
def inspect_power_query(
    instance: str,
    aspect: Literal[
        "queries", "refresh_health", "query_diagnostics", "connections", "sql_queries",
        "folding_risk", "sharepoint_patterns", "dependencies", "mashup_traces",
    ],
    table: str = "",
    include_steps: bool = False,
    since_minutes: int = 1440,
) -> str:
    """Diagnose Power Query and the data connections (SQL Server, SharePoint, OneDrive files).
    `aspect`: refresh_health (which tables errored on last refresh / are stale -- check this first),
    queries (M code per table plus shared queries; `table` filters, `include_steps` adds named steps),
    connections (each source classified, gateway need, risky patterns), sql_queries (lint embedded
    SQL: TOP caps, SELECT *, truncating CASTs, year-specific tables), folding_risk (static estimate of
    where query folding stops; not proof), sharepoint_patterns (duplicate list loads, whole-site file
    listings, swallowed `try ... otherwise null` errors), dependencies (what feeds what; which tables
    fail if a source is down), mashup_traces (Power BI's own logs of the last `since_minutes`: request
    counts, HTTP failures, SQL rows read), query_diagnostics (only if the user recorded a Query
    Diagnostics session)."""
    tbl = table or None
    if aspect == "queries":
        result = base.list_power_query(instance, tbl, include_steps)
    elif aspect == "refresh_health":
        result = base.diagnose_power_query(instance)
    elif aspect == "query_diagnostics":
        result = base.find_query_diagnostics(instance, 200)
    elif aspect == "connections":
        result = base.classify_connections(instance)
    elif aspect == "sql_queries":
        result = base.analyze_sql_queries(instance)
    elif aspect == "folding_risk":
        result = base.analyze_folding_risk(instance, include_steps)
    elif aspect == "sharepoint_patterns":
        result = base.analyze_sharepoint_patterns(instance, True)
    elif aspect == "dependencies":
        result = base.check_refresh_dependencies(instance)
    else:  # mashup_traces
        result = base.read_mashup_traces(instance or None, max(1, since_minutes))
    return shrink(result)


@_tool()
def check_data_quality(
    instance: str,
    check: Literal["relationship_integrity", "profile_table", "unused_fields"],
    table: str = "",
    key_columns: str = "",
    columns: str = "",
    expected_row_count: int = -1,
    pbix_path: str = "",
) -> str:
    """Data-correctness probes on the LOADED model (never the source systems). `check`:
    relationship_integrity (key values with no match across each relationship -- the (Blank) rows that
    silently distort totals), profile_table (needs `table`: blanks, distinct counts, min/max,
    truncated text, placeholder dates; `key_columns` and `columns` are comma-separated;
    `expected_row_count` reconciles against a source COUNT(*), leave -1 to skip),
    unused_fields (visible columns/measures no visual, filter or DAX uses; a lead, not a delete list;
    needs `pbix_path` from list_open_files)."""
    if check == "relationship_integrity":
        result = base.check_relationship_integrity(instance)
    elif check == "profile_table":
        _need(table, "table", check)
        result = base.profile_table(
            instance, table, _split(columns), _split(key_columns),
            expected_row_count if expected_row_count >= 0 else None,
        )
    else:  # unused_fields
        _need(pbix_path, "pbix_path", check)
        result = base.find_unused_fields(instance, require_pbix(pbix_path))
    return shrink(result)


@_tool()
def get_report_layout(pbix_path: str) -> str:
    """Read a .pbix's report layout offline (Power BI Desktop need not be running): pages, visuals, the
    fields each uses, and filters at report/page/visual level (`contains_negation: true` silently excludes
    values), plus hidden/drillthrough/tooltip pages, hidden or off-canvas visuals, bookmarks and slicer
    sync groups. Does not include the data model."""
    return shrink(base.get_report_layout(require_pbix(pbix_path)))


@_tool()
def compare_dashboards(left: str, right: str) -> str:
    """Diff two dashboards (backup vs current, before vs after a change). Each side is an open file's
    `instance` name or a .pbix path. Two open files: tables, columns, measures (DAX), relationships,
    RLS roles and Power Query steps. Two .pbix paths: report pages, visuals and filters only."""
    for side in (left, right):
        if _looks_like_path(side):
            require_pbix(side, "left/right")
    return shrink(base.compare_dashboards(left, right))


@_tool()
def review_file(path: str, member: str = "", offset: int = 0, max_chars: int = 6000) -> str:
    """Read one Power BI file's TEXT without changing anything: .pbip .pbir .pbids .rdl .tmdl, a PBIR .json
    inside a *.Report folder, or a part of a .pbix/.pbit. For a .pbix/.pbit call it first WITHOUT `member`
    to list the parts, then with `member` (for example Report/Layout) to read one; the compressed model
    (DataModel) and Power Query container (DataMashup) cannot be read. Returns whether the file is
    well-formed, whether it sits in the edit folder, and a window of the text (`offset`, `max_chars`;
    `truncated` means more follows). Passwords and tokens appear as [REDACTED]."""
    return shrink(editing.review(path, member, max(0, offset), max_chars))


@_write_tool()
def propose_change(path: str, old_text: str, new_text: str, reason: str, member: str = "", expected_count: int = 1) -> str:
    """STEP 1 of any edit. Proposes replacing exact `old_text` with `new_text` in a file in the edit folder
    (.pbix .pbit .pbip .pbir .pbids .rdl .tmdl or PBIR .json). Changes NOTHING: it validates the result and
    returns a diff, objects affected and risk. For .pbix/.pbit pass `member` (a report-layer part from
    review_file). `old_text` must match exactly `expected_count` places (default 1); quote enough
    surrounding text. State the problem in `reason`. Then show the user the diff verbatim and wait for
    their explicit approval. Originals outside the edit folder are never edited."""
    return shrink(editing.propose(path, old_text, new_text, reason, member, max(1, expected_count)))


@_write_tool()
def finish_change(change_id: str, action: Literal["apply", "rollback"], approval_code: str = "") -> str:
    """STEP 2 (apply) or UNDO (rollback) for a change from propose_change. Call ONLY after the user has
    explicitly approved THAT change in chat and read you the 6-digit approval code shown in the server
    window on the PC; never guess or invent a code, and never infer approval. With an empty
    `approval_code` it issues a fresh code and applies nothing. apply: backs up the whole file, applies
    only the approved edit, re-verifies, and restores the original automatically if verification fails.
    rollback: restores the pre-change backup (needs its own code; refuses if the file changed since)."""
    return shrink(editing.finish(change_id, action, approval_code))


@_tool()
def run_dax_query(instance: str, dax_query: str, max_rows: int = 100) -> str:
    """Run a read-only DAX query (must start with EVALUATE, optionally after DEFINE) against an open file:
    ad hoc aggregations, top-N, DISTINCT lists, testing a candidate measure. Returns
    {rows, row_count, truncated}. If `truncated` is true there were MORE rows -- never conclude
    'that is everything'; aggregate in DAX instead. Prefer aggregates over listing individual records."""
    require_read_only_dax(dax_query)
    cap = max(1, min(max_rows, MAX_DAX_ROWS))
    return shrink(base.run_dax_query(instance, dax_query, cap))
