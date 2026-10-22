"""audit_dashboard: run every check and return ONE ranked list of findings.

Each check runs independently; a failure in one is reported in
`checks_failed` and never hides the others. Findings are ranked
high > medium > info and tagged with the tool to call for the full detail.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from . import analyzers, insight, layout, metadata, quality
from .discovery import PbiInstance
from .engine import _NO_MODEL_MESSAGE, ModelNotLoadedError, model_loaded

_ORDER = {"high": 0, "medium": 1, "info": 2}


def _f(sev: str, category: str, tool: str, message: str, where: str | None = None) -> dict[str, Any]:
    return {"severity": sev, "category": category, "message": message, "where": where, "drill_down": tool}


def audit_dashboard(
    instance: PbiInstance,
    pbix_path: str | None = None,
    include_data_quality: bool = True,
    include_traces: bool = True,
) -> dict[str, Any]:
    if model_loaded(instance.port) is False:
        raise ModelNotLoadedError(_NO_MODEL_MESSAGE)
    pbix_path = pbix_path or instance.file_path
    findings: list[dict[str, Any]] = []
    ran: list[str] = []
    failed: list[dict[str, str]] = []

    def step(name: str, fn):
        try:
            out = fn()
            ran.append(name)
            return out
        except Exception as exc:  # noqa: BLE001
            failed.append({"check": name, "error": (str(exc).splitlines() or [""])[0][:200]})
            return None

    def from_analyzer(res: dict[str, Any] | None, category: str, tool: str, min_sev: str = "info") -> None:
        if not res:
            return
        for f in res.get("findings", []):
            if _ORDER[f["severity"]] <= _ORDER[min_sev]:
                where = f.get("query") or f.get("connection") or f.get("relationship") or f.get("source")
                findings.append(_f(f["severity"], category, tool, f["message"], where if isinstance(where, str) else None))

    # ---- refresh health -------------------------------------------------
    d = step("diagnose_power_query", lambda: metadata.diagnose_power_query(instance))
    if d:
        for p in d["partitions"]:
            if p["has_error"]:
                findings.append(_f("high", "refresh", "diagnose_power_query", f"Last refresh of '{p['table']}' failed: {p['error_message'][:240]}", p["table"]))
            elif p.get("refreshed_before_last_edit"):
                findings.append(_f("medium", "refresh", "diagnose_power_query", f"'{p['table']}' was edited after its last refresh: the loaded data doesn't reflect its current query.", p["table"]))

    # ---- DAX validity, model structure ---------------------------------------
    dax = step("list_all_dax_expressions", lambda: metadata.list_all_dax_expressions(instance))
    if dax:
        for m in dax["broken_measures"]:
            findings.append(_f("high", "dax", "list_all_dax_expressions", f"Measure '{m['measure']}' is in state {m['state']!r}: the engine considers its DAX broken.", f"{m['table']}[{m['measure']}]"))
    rel = step("analyze_relationships", lambda: metadata.analyze_relationships(instance))
    if rel:
        if rel["many_to_many_count"]:
            findings.append(_f("info", "model", "analyze_relationships", f"{rel['many_to_many_count']} many-to-many relationship(s): ambiguous filtering and fan-out risk; confirm each is intentional."))
        if rel["bidirectional_count"]:
            findings.append(_f("info", "model", "analyze_relationships", f"{rel['bidirectional_count']} bidirectional cross-filter relationship(s): can create ambiguous paths and slow queries."))
        if rel["inactive_count"]:
            findings.append(_f("info", "model", "analyze_relationships", f"{rel['inactive_count']} inactive relationship(s): only used if a measure calls USERELATIONSHIP; otherwise dead weight."))
        dax_tables: set[str] = set()
        try:
            texts = [m["dax"] for m in (dax or {}).get("measures", [])] + [c["dax"] for c in (dax or {}).get("calculated_columns", [])] \
                + [t["dax"] for t in (dax or {}).get("calculated_tables", [])]
            sec_ = metadata.list_security_roles(instance)
            texts += [tp["filter_expression"] for r in sec_["roles"] for tp in r["table_permissions"]]
            dax_tables = {t for t, _ in metadata.dax_references(texts)[0]}
        except Exception:  # noqa: BLE001
            pass
        for t in rel["isolated_visible_tables"]:
            if t in dax_tables:
                findings.append(_f("info", "model", "analyze_relationships", f"Table '{t}' has no relationships but is referenced from DAX/RLS (a lookup or security table): expected.", t))
            else:
                findings.append(_f("medium", "model", "analyze_relationships", f"Visible table '{t}' has no relationships and no DAX/RLS references it: it can't filter or be filtered, so it may be dead weight (or a loaded navigation table).", t))

    # ---- security ---------------------------------------------------------------
    sec = step("list_security_roles", lambda: metadata.list_security_roles(instance))
    if sec:
        for r in sec["roles"]:
            for tp in r["table_permissions"]:
                if tp["has_error"]:
                    findings.append(_f("high", "security", "list_security_roles", f"RLS filter for role '{r['role']}' on '{tp['table']}' has an error: {tp['error_message'][:160]}", r["role"]))
            if r["unfiltered_tables"]:
                findings.append(_f("info", "security", "list_security_roles", f"Role '{r['role']}' sees every row of: {r['unfiltered_tables']}.", r["role"]))
        if sec["role_count"] == 0:
            findings.append(_f("info", "security", "list_security_roles", "No row-level security roles are defined: every viewer of this model sees all data."))

    # ---- Power Query layer (connections, SQL, folding, SharePoint, dependencies) ---------
    queries = step("query_graph", lambda: insight.get_queries(instance)[0])
    if queries is not None:
        from_analyzer(step("classify_connections", lambda: analyzers.classify_connections(queries)), "connections", "classify_connections")
        from_analyzer(step("analyze_sql_queries", lambda: analyzers.analyze_sql_queries(queries)), "sql", "analyze_sql_queries")
        fold = step("analyze_folding_risk", lambda: analyzers.analyze_folding(queries))
        from_analyzer(fold, "folding", "analyze_folding_risk", min_sev="medium")
        if fold:
            info_n = sum(1 for f in fold["findings"] if f["severity"] == "info")
            if info_n:
                findings.append(_f("info", "folding", "analyze_folding_risk", f"{info_n} query(ies) run entirely locally (native SQL, SharePoint or file sources: no folding). See analyze_folding_risk for the list."))
        from_analyzer(step("analyze_sharepoint_patterns", lambda: analyzers.analyze_sharepoint(queries)), "sharepoint", "analyze_sharepoint_patterns")
        from_analyzer(step("check_refresh_dependencies", lambda: analyzers.refresh_dependencies(queries, metadata._is_diagnostics_name)), "dependencies", "check_refresh_dependencies")

    # ---- report layout + field usage ----------------------------------------------------
    lay = step("get_report_layout", lambda: layout.parse_layout(pbix_path)) if pbix_path else None
    if pbix_path is None:
        failed.append({"check": "get_report_layout", "error": "no pbix_path (open the file directly or pass pbix_path)"})
    if lay:
        sm = lay["summary"]
        if sm["hidden_pages"]:
            findings.append(_f("info", "report", "get_report_layout", f"{len(sm['hidden_pages'])} hidden page(s) (reachable only via drillthrough/buttons/bookmarks): {sm['hidden_pages'][:6]}"))
        if sm["off_canvas_visuals"]:
            findings.append(_f("medium", "report", "get_report_layout", f"{len(sm['off_canvas_visuals'])} visual(s) positioned outside their page: invisible content still running queries."))
        if sm["slicer_sync_groups"]:
            findings.append(_f("info", "report", "get_report_layout", f"Slicer sync across pages: {sm['slicer_sync_groups']}. A slicer change on one page silently filters the others."))
        for f in lay["report_filters"]:
            findings.append(_f("medium", "report", "get_report_layout", f"Report-level filter on {f.get('field_refs') or f.get('fields')} applies to EVERY page (negation={f.get('contains_negation')}, values={f.get('values')[:6]}).", "report"))
        for p in lay["pages"]:
            for f in p["filters"]:
                if f.get("contains_negation"):
                    findings.append(_f("medium", "report", "get_report_layout", f"Page '{p['display_name']}' excludes {f.get('values')[:8]} from {f.get('field_refs') or f.get('fields')}.", p["display_name"]))
            for v in p["visuals"]:
                for f in v.get("filters") or []:
                    if f.get("contains_negation") and f.get("values"):
                        findings.append(_f("medium", "report", "get_report_layout",
                                           f"Visual '{v.get('title') or v.get('visual_type')}' on '{p['display_name']}' silently excludes {f['values'][:8]} from {f.get('field_refs') or f.get('fields')}.",
                                           p["display_name"]))
        un = step("find_unused_fields", lambda: metadata.find_unused_fields(instance, pbix_path))
        if un:
            c = un["counts"]
            size = sum(x.get("approx_bytes") or 0 for x in un["unused_visible_columns"])
            if c["unused_columns"]:
                findings.append(_f("medium" if size > 5_000_000 else "info", "efficiency", "find_unused_fields",
                                   f"{c['unused_columns']} visible column(s) are used by no visual, filter, DAX, relationship or RLS rule; they hold roughly {size/1e6:.1f} MB. Largest: "
                                   + ", ".join(f"{x['table']}[{x['column']}]" for x in un["unused_visible_columns"][:4])))
            if c["unused_measures"]:
                findings.append(_f("info", "efficiency", "find_unused_fields", f"{c['unused_measures']} measure(s) are used by nothing: {[x['measure'] for x in un['unused_visible_measures'][:5]]}"))

    size = step("analyze_model_size", lambda: metadata.analyze_model_size(instance, top_n=5))
    if size and size["tables_by_size"]:
        top = size["tables_by_size"][0]
        findings.append(_f("info", "efficiency", "analyze_model_size", f"Model is {size['total_allocated_bytes']/1e6:.1f} MB; largest table '{top['table']}' is {top['allocated_bytes']/1e6:.1f} MB ({100*top['allocated_bytes']/max(size['total_allocated_bytes'],1):.0f}%)."))

    # ---- data quality ---------------------------------------------------------------------
    if include_data_quality:
        integ = step("check_relationship_integrity", lambda: quality.check_relationship_integrity(instance))
        if integ:
            for f in integ["findings"]:
                if f["severity"] != "info":
                    findings.append(_f(f["severity"], "data_quality", "check_relationship_integrity", f["message"], f.get("relationship")))

    # ---- runtime evidence from Power BI's own logs -----------------------------------------
    if include_traces:
        tr = step("read_mashup_traces", lambda: insight.read_mashup_traces(instance, since_minutes=1440))
        if tr and tr.get("events_parsed"):
            sp = tr["sharepoint"]
            for L in sp["lists"]:
                if L["failed_requests"]:
                    where = "this model" if L["in_open_model"] else "another file opened in the log window"
                    sev = "high" if L["in_open_model"] else "info"
                    q = f" ({', '.join(L['queries'])})" if L["queries"] else ""
                    findings.append(_f(sev, "runtime", "read_mashup_traces", f"SharePoint list {L['list_id'][:8]}...{q} returned errors on {L['failed_requests']} of {L['requests']} requests ({where}).", L["list_id"]))
            if sp["navigation_evaluations"] >= 5:
                findings.append(_f("info", "runtime", "read_mashup_traces", f"The SharePoint navigation table was fetched {sp['navigation_fetches']} times across {sp['navigation_evaluations']} evaluations in the log window."))
            for g in tr["folding_failures"]["groups"]:
                if g["level"] in ("Error", "Warning"):
                    findings.append(_f("info", "runtime", "read_mashup_traces", f"Engine reported {g['count']}x folding failure ({g['operation']}) {g['message'][:80]} -- sources in same evaluation: {g['sources_seen_in_same_evaluation']}."))
            if tr["container_crashes"]["count"]:
                findings.append(_f("medium", "runtime", "read_mashup_traces", f"{tr['container_crashes']['count']} mashup-container crash(es) logged: {tr['container_crashes']['samples'][0]['message'][:140]}. Usually Power BI Desktop was updated while running; close all instances and reopen."))
            big = [c for c in tr["sql"]["commands"] if (c["rows_read"] or 0) >= 100_000]
            for c in big[:3]:
                findings.append(_f("info", "runtime", "read_mashup_traces", f"SQL pulled {c['rows_read']:,} rows in one command: {c['command_preview'][:90]}"))

    findings.sort(key=lambda f: _ORDER[f["severity"]])
    by_cat = Counter((f["category"], f["severity"]) for f in findings)
    return {
        "file": instance.display_name,
        "summary": {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "info")},
        "top_priorities": [f for f in findings if f["severity"] != "info"][:15],
        "findings": findings,
        "by_category": {c: {s: n for (cc, s), n in by_cat.items() if cc == c} for c in sorted({c for c, _ in by_cat})},
        "checks_run": ran,
        "checks_failed": failed,
        "not_checked": [
            "Whether each SharePoint request folded or how it paged (request bodies aren't logged; see read_mashup_traces limits)",
            "DAX query performance (would need a server-timings trace)",
            "Row-by-row equality with the source systems (needs source credentials; use profile_table(expected_row_count=...) with a count you fetch yourself)",
            "Bookmarks, hidden visuals and tooltip pages (detection exists but has never seen a real example)",
            "Dashboards other than this one (use compare_dashboards, or open them and audit each)",
        ],
    }
