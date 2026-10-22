"""Instance-level entry points for the M-layer analyzers and trace reader."""
from __future__ import annotations

from typing import Any

from . import analyzers, metadata, qmodel, traces
from .discovery import PbiInstance
from .qmodel import Q


def get_queries(instance: PbiInstance) -> tuple[dict[str, Q], dict[str, str]]:
    """Build the query graph for the open file. Query Diagnostics output is
    excluded: it reads a temp trace file and would raise false alarms."""
    pq = metadata.list_power_query(instance, include_steps=True)
    return qmodel.build_queries(pq, exclude=metadata._is_diagnostics_name)


def classify_connections(instance: PbiInstance) -> dict[str, Any]:
    queries, _ = get_queries(instance)
    return analyzers.classify_connections(queries)


def analyze_sql_queries(instance: PbiInstance) -> dict[str, Any]:
    queries, _ = get_queries(instance)
    return analyzers.analyze_sql_queries(queries)


def analyze_folding_risk(instance: PbiInstance, include_steps: bool = False) -> dict[str, Any]:
    queries, _ = get_queries(instance)
    return analyzers.analyze_folding(queries, include_steps)


def analyze_sharepoint_patterns(instance: PbiInstance, include_row_counts: bool = True) -> dict[str, Any]:
    queries, _ = get_queries(instance)
    res = analyzers.analyze_sharepoint(queries)
    if include_row_counts:
        rows = []
        for name, q in queries.items():
            if not q.loaded:
                continue
            if not any(c["kind"] in ("sharepoint_site", "onedrive_personal", "sharepoint_web_file", "onedrive_web_file")
                       for c in qmodel.all_sources(queries, name).values()):
                continue
            try:
                n = metadata.table_row_count(instance, name)
            except Exception as exc:  # noqa: BLE001
                rows.append({"table": name, "rows": None, "error": str(exc)[:120]})
                continue
            rows.append({"table": name, "rows": n})
            if n >= 5000 and n % 5000 == 0:
                res["findings"].append(analyzers._finding(
                    "info", "row_count_multiple_of_5000",
                    f"'{name}' has exactly {n:,} rows, a multiple of 5,000 (SharePoint's default list-view threshold). Compare with the list's item count in SharePoint to rule out a cut-off.", query=name))
        res["loaded_row_counts"] = rows
        res["summary"] = analyzers._summary(res["findings"])
        res["findings"] = analyzers._sorted(res["findings"])
    return res


def check_refresh_dependencies(instance: PbiInstance) -> dict[str, Any]:
    queries, _ = get_queries(instance)
    return analyzers.refresh_dependencies(queries, metadata._is_diagnostics_name)


def read_mashup_traces(instance: PbiInstance | None = None, since_minutes: int = 1440) -> dict[str, Any]:
    """Read Power BI's mashup-engine trace logs (see traces.py). If an open
    instance is given, SharePoint list GUIDs in the logs are matched against
    the lists that model actually pulls, so failures can be attributed to
    named queries (logs cover every file opened in the window, not just one)."""
    known: dict[str, list[str]] = {}
    if instance is not None:
        queries, _ = get_queries(instance)
        for p in analyzers.analyze_sharepoint(queries)["list_and_file_pulls"]:
            if p["identifier"].startswith("Id="):
                known[p["identifier"][3:].lower()] = p["queries"]
    events, meta = traces.read_events(since_minutes)
    out = traces.analyze(events, meta, known)
    if instance is not None:
        # Row counts of loaded staging tables let the reader judge whether
        # "several requests to one list" could plausibly be paging.
        queries, _ = get_queries(instance)
        for row in out.get("sharepoint", {}).get("lists", []):
            for qn in row["queries"]:
                if qn in queries and queries[qn].loaded:
                    try:
                        row["rows_in_model"] = metadata.table_row_count(instance, qn)
                    except Exception:  # noqa: BLE001
                        pass
                    break
    if not meta["trace_dirs"]:
        out["note"] = ("No Power BI trace folder found. Expected %USERPROFILE%\\Microsoft\\Power BI Desktop Store App\\Traces. "
                       "If tracing is off: Power BI Desktop > Options > Diagnostics > enable tracing, then refresh and call this again.")
    return out
