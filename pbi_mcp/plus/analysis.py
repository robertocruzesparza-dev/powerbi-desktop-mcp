"""Project-level analysis: unused fields, documentation, project-to-project diff, source lineage."""
from __future__ import annotations

import re
from typing import Any

from . import bpa, checks, daxrefs, pbir
from .model import Model
from .project import Project

_SOURCES = [
    ("SQL Server", re.compile(r"Sql\.Database\s*\(\s*\"([^\"]*)\"\s*,\s*\"([^\"]*)\"")),
    ("SQL Server (native)", re.compile(r"Value\.NativeQuery\s*\(")),
    ("SharePoint list", re.compile(r"SharePoint\.Tables\s*\(\s*\"([^\"]*)\"")),
    ("SharePoint files", re.compile(r"SharePoint\.(?:Files|Contents)\s*\(\s*\"([^\"]*)\"")),
    ("Excel", re.compile(r"Excel\.Workbook\s*\(")),
    ("CSV/Text", re.compile(r"Csv\.Document\s*\(")),
    ("Web", re.compile(r"Web\.(?:Contents|BrowserContents)\s*\(\s*\"([^\"]*)\"")),
    ("OData", re.compile(r"OData\.Feed\s*\(\s*\"([^\"]*)\"")),
    ("Folder", re.compile(r"Folder\.(?:Files|Contents)\s*\(\s*\"([^\"]*)\"")),
    ("Inline table", re.compile(r"#table\s*\(")),
    ("Dataflow/Fabric", re.compile(r"(PowerPlatform\.Dataflows|Lakehouse\.Contents|Fabric\.)")),
]


def _redact(target: str | None) -> str | None:
    """Drop query strings, fragments and user info from a URL-like source target (they often hold keys or tokens)."""
    if not target:
        return target
    import re as _re
    t = _re.sub(r"[?#].*$", "", target)
    return _re.sub(r"//[^/@]*@", "//", t)


def classify_sources(model: Model) -> list[dict[str, Any]]:
    """Which M source each table / shared query loads from (static text match)."""
    items: list[tuple[str, str]] = []
    for t in model.tables():
        for p in t.node.kids("partition"):
            if (p.expr or "").strip() == "m" and "source" in p.props:
                items.append((t.node.name, p.props["source"].value))
    for e in model.shared_expressions():
        items.append((f"query:{e.node.name}", e.node.expr or ""))
    out = []
    for owner, m in items:
        kinds = []
        for label, pat in _SOURCES:
            mm = pat.search(m)
            if mm:
                kinds.append({"type": label, "target": _redact(mm.group(1)) if mm.groups() else None})
        out.append({"owner": owner, "sources": kinds or [{"type": "unrecognised", "target": None}]})
    return out


def unused_fields(project: Project) -> dict[str, Any]:
    if project.tmdl_dir is None:
        raise ValueError("Needs a TMDL semantic model.")
    model = Model.load(project.tmdl_dir)
    used_in_report: set[tuple[str, str]] = set()
    report_known = project.pbir_dir is not None
    if report_known:
        rep = pbir.Report.load(project.pbir_dir)
        for rel, data in rep.files.items():
            for kind, table, name in pbir.unique_fields(data):
                used_in_report.add((table.lower(), name.split(".")[0].lower()))
    used_in_dax: set[tuple[str, str]] = set()
    measure_names = {loc.node.name.lower(): t for t, loc in model.measures()}
    for label, owner, expr in model.all_dax():
        for r in daxrefs.refs(expr):
            if r.table:
                used_in_dax.add((r.table.lower(), r.name.lower()))
            else:
                used_in_dax.add((measure_names.get(r.name.lower(), owner or "").lower(), r.name.lower()))
                if owner:
                    used_in_dax.add((owner.lower(), r.name.lower()))
    structural: set[tuple[str, str]] = set()
    for r in model.relationships():
        structural.add(((r["from_table"] or "").lower(), (r["from_column"] or "").lower()))
        structural.add(((r["to_table"] or "").lower(), (r["to_column"] or "").lower()))
    for t in model.tables():
        for c in t.node.kids("column"):
            sbc = c.prop("sortByColumn")
            if sbc:
                structural.add((t.node.name.lower(), sbc.strip("'").lower()))
        for h in t.node.kids("hierarchy"):
            for lv in h.kids("level"):
                col = lv.prop("column")
                if col:
                    structural.add((t.node.name.lower(), col.strip("'").lower()))
    unused_cols, unused_meas, dax_only = [], [], []
    for t in model.tables():
        tn = t.node.name
        for c in t.node.kids("column"):
            key = (tn.lower(), c.name.lower())
            if key in used_in_report or key in structural:
                continue
            entry = f"{tn}[{c.name}]"
            (dax_only if key in used_in_dax else unused_cols).append({"field": entry, "hidden": c.flag("isHidden"), "calculated": c.expr is not None})
        for m in t.node.kids("measure"):
            key = (tn.lower(), m.name.lower())
            if key in used_in_report:
                continue
            entry = f"{tn}[{m.name}]"
            (dax_only if key in used_in_dax else unused_meas).append({"field": entry, "hidden": m.flag("isHidden"), "calculated": True})
    return {"report_checked": report_known, "unused_columns": unused_cols, "unused_measures": unused_meas, "used_only_by_other_dax": dax_only,
            "caveat": "Leads, not a delete list: this cannot see tooltips/Q&A/bookmarked states created in the service, personalised visuals, "
                      "Analyze-in-Excel, or other reports connected to this model." + ("" if report_known else " No PBIR report was found, so report usage was NOT considered; every field not used by DAX is listed.")}


def document(project: Project, include_dax: bool = True) -> str:
    lines: list[str] = [f"# {project.name}: Power BI project documentation", ""]
    model = Model.load(project.tmdl_dir) if project.tmdl_dir else None
    if model is None:
        lines += ["_No TMDL semantic model found; only the report is documented._", ""]
    else:
        d = model.to_dict()
        c = d["counts"]
        lines += [f"**Model:** {c['tables']} tables, {c['columns']} columns, {c['measures']} measures, {c['relationships']} relationships, "
                  f"{len(d['roles'])} roles, culture `{d['model']['culture']}`.", ""]
        lines += ["## Data sources", "", "| Table / query | Source type | Target |", "|---|---|---|"]
        for s in classify_sources(model):
            for src in s["sources"]:
                lines.append(f"| {s['owner']} | {src['type']} | {src['target'] or ''} |")
        lines += ["", "## Tables", ""]
        for t in d["tables"]:
            flags = [f for f, on in (("hidden", t["hidden"]), ("calculated", t["calculated"])) if on]
            lines.append(f"### {t['name']}" + (f" ({', '.join(flags)})" if flags else ""))
            if t["description"]:
                lines.append(f"\n{t['description']}")
            if t["columns"]:
                lines += ["", "| Column | Type | Hidden | Notes |", "|---|---|---|---|"]
                for col in t["columns"]:
                    note = "calculated: `" + " ".join((col["expression"] or "").split())[:120] + "`" if col["calculated"] else (col["description"] or "")
                    lines.append(f"| {col['name']} | {col['data_type'] or ''} | {'yes' if col['hidden'] else ''} | {note} |")
            lines.append("")
        lines += ["## Measures", ""]
        for t in d["tables"]:
            for m in t["measures"]:
                lines.append(f"### {t['name']}[{m['name']}]")
                bits = [b for b in (f"format `{m['format']}`" if m["format"] else "", f"folder {m['display_folder']}" if m["display_folder"] else "",
                                    "hidden" if m["hidden"] else "") if b]
                if bits:
                    lines.append(", ".join(bits))
                if m["description"]:
                    lines.append(f"\n{m['description']}")
                if include_dax:
                    lines += ["", "```dax", m["expression"], "```"]
                lines.append("")
        graph = model.measure_graph()
        lines += ["## Measure dependencies", ""]
        for k, v in graph["edges"].items():
            if v["measures"]:
                lines.append(f"- `{k}` uses " + ", ".join(f"`{x}`" for x in v["measures"]))
        lines += ["", "## Relationships", "", "| From | To | Direction | Active |", "|---|---|---|---|"]
        for r in d["relationships"]:
            lines.append(f"| {r['from_table']}[{r['from_column']}] | {r['to_table']}[{r['to_column']}] | {r['cross_filter']} | {'yes' if r['active'] else 'no'} |")
        if d["roles"]:
            lines += ["", "## Row-level security", ""]
            for r in d["roles"]:
                lines.append(f"- **{r['name']}**: " + "; ".join(f"{p['table']}: `{(p['filter'] or '').strip()}`" for p in r["permissions"]))
    if project.pbir_dir is not None:
        rep = pbir.Report.load(project.pbir_dir).summary()
        lines += ["", "## Report", "", f"{rep['page_count']} pages, {rep['bookmark_count']} bookmarks.", ""]
        for pg in rep["pages"]:
            lines.append(f"### {pg['display_name']}" + (" (hidden)" if pg["hidden"] else ""))
            lines += ["", "| Visual | Type | Fields |", "|---|---|---|"]
            for v in pg["visuals"]:
                lines.append(f"| {v['title'] or v['id']} | {v['type']} | {', '.join(v['fields'])} |")
            lines.append("")
    v = checks.validate_project(project)
    lines += ["## Validation", "", f"{v['counts']['high']} high, {v['counts']['medium']} medium, {v['counts']['info']} info finding(s)."]
    if model is not None:
        b = bpa.run(model)
        lines.append(f"Best-practice scan: {b['summary']['high']} high, {b['summary']['medium']} medium, {b['summary']['info']} info.")
    return "\n".join(lines) + "\n"


def diff_projects(a: Project, b: Project) -> dict[str, Any]:
    out: dict[str, Any] = {"model": {}, "report": {}}
    if a.tmdl_dir and b.tmdl_dir:
        da, db = Model.load(a.tmdl_dir).to_dict(), Model.load(b.tmdl_dir).to_dict()
        ta, tb = {t["name"]: t for t in da["tables"]}, {t["name"]: t for t in db["tables"]}
        out["model"]["tables_added"] = sorted(set(tb) - set(ta))
        out["model"]["tables_removed"] = sorted(set(ta) - set(tb))
        cols_changed, meas_changed = [], []
        for name in sorted(set(ta) & set(tb)):
            ca, cb = {c["name"]: c for c in ta[name]["columns"]}, {c["name"]: c for c in tb[name]["columns"]}
            for x in sorted(set(cb) - set(ca)):
                cols_changed.append({"column": f"{name}[{x}]", "change": "added"})
            for x in sorted(set(ca) - set(cb)):
                cols_changed.append({"column": f"{name}[{x}]", "change": "removed"})
            for x in sorted(set(ca) & set(cb)):
                for key in ("data_type", "hidden", "format", "expression", "summarize_by", "sort_by"):
                    if ca[x][key] != cb[x][key]:
                        cols_changed.append({"column": f"{name}[{x}]", "change": key, "from": ca[x][key], "to": cb[x][key]})
            ma, mb = {m["name"]: m for m in ta[name]["measures"]}, {m["name"]: m for m in tb[name]["measures"]}
            for x in sorted(set(mb) - set(ma)):
                meas_changed.append({"measure": f"{name}[{x}]", "change": "added"})
            for x in sorted(set(ma) - set(mb)):
                meas_changed.append({"measure": f"{name}[{x}]", "change": "removed"})
            for x in sorted(set(ma) & set(mb)):
                for key in ("expression", "format", "hidden", "display_folder"):
                    if ma[x][key] != mb[x][key]:
                        meas_changed.append({"measure": f"{name}[{x}]", "change": key, "from": ma[x][key], "to": mb[x][key]})
        out["model"]["columns"] = cols_changed
        out["model"]["measures"] = meas_changed

        def rk(r):
            return (r["from_table"], r["from_column"], r["to_table"], r["to_column"])
        ra, rb = {rk(r): r for r in da["relationships"]}, {rk(r): r for r in db["relationships"]}
        out["model"]["relationships_added"] = [list(k) for k in sorted(set(rb) - set(ra))]
        out["model"]["relationships_removed"] = [list(k) for k in sorted(set(ra) - set(rb))]
        out["model"]["relationships_changed"] = [list(k) for k in sorted(set(ra) & set(rb))
                                                  if (ra[k]["cross_filter"], ra[k]["active"]) != (rb[k]["cross_filter"], rb[k]["active"])]
        roles_a = {r["name"]: r for r in da["roles"]}
        roles_b = {r["name"]: r for r in db["roles"]}
        out["model"]["roles_added"] = sorted(set(roles_b) - set(roles_a))
        out["model"]["roles_removed"] = sorted(set(roles_a) - set(roles_b))
        out["model"]["roles_changed"] = sorted(n for n in set(roles_a) & set(roles_b) if roles_a[n]["permissions"] != roles_b[n]["permissions"])
    if a.pbir_dir and b.pbir_dir:
        sa, sb = pbir.Report.load(a.pbir_dir).summary(), pbir.Report.load(b.pbir_dir).summary()
        pa, pb = {p["display_name"]: p for p in sa["pages"]}, {p["display_name"]: p for p in sb["pages"]}
        out["report"]["pages_added"] = sorted(set(pb) - set(pa))
        out["report"]["pages_removed"] = sorted(set(pa) - set(pb))
        vis_changes = []
        for name in sorted(set(pa) & set(pb)):
            va = {v["id"]: v for v in pa[name]["visuals"]}
            vb = {v["id"]: v for v in pb[name]["visuals"]}
            for x in sorted(set(vb) - set(va)):
                vis_changes.append({"page": name, "visual": x, "change": "added", "type": vb[x]["type"]})
            for x in sorted(set(va) - set(vb)):
                vis_changes.append({"page": name, "visual": x, "change": "removed", "type": va[x]["type"]})
            for x in sorted(set(va) & set(vb)):
                for key in ("type", "title", "fields", "x", "y", "width", "height", "hidden"):
                    if va[x][key] != vb[x][key]:
                        vis_changes.append({"page": name, "visual": x, "change": key, "from": va[x][key], "to": vb[x][key]})
        out["report"]["visuals"] = vis_changes
    return out


def pq_from_model(model: Model) -> dict[str, Any]:
    """The same dict shape metadata.list_power_query returns for a live file, built from TMDL text."""
    table_queries, shared = [], []
    for t in model.tables():
        for p in t.node.kids("partition"):
            kind = (p.expr or "").strip()
            if kind == "m" and "source" in p.props:
                table_queries.append({"table": t.node.name, "partition_name": p.name, "m_query": p.props["source"].value, "is_m": True})
    for e in model.shared_expressions():
        shared.append({"name": e.node.name, "m_query": e.node.expr or ""})
    return {"table_queries": table_queries, "shared_queries_and_parameters": shared}


def analyze_queries(model: Model, aspect: str = "all", include_steps: bool = False) -> dict[str, Any]:
    """Run the existing M-layer analyzers (connections, embedded SQL, folding, SharePoint, refresh graph) on a saved project."""
    from .. import analyzers, qmodel
    queries, params = qmodel.build_queries(pq_from_model(model))
    table = {
        "connections": lambda: analyzers.classify_connections(queries),
        "sql": lambda: analyzers.analyze_sql_queries(queries),
        "folding": lambda: analyzers.analyze_folding(queries, include_steps),
        "sharepoint": lambda: analyzers.analyze_sharepoint(queries),
        "dependencies": lambda: analyzers.refresh_dependencies(queries, lambda name: False),
    }
    if aspect != "all" and aspect not in table:
        raise ValueError(f"aspect must be one of: all, {', '.join(table)}")
    chosen = table if aspect == "all" else {aspect: table[aspect]}
    out: dict[str, Any] = {"query_count": len(queries), "parameters": params}
    for key, fn in chosen.items():
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001 -- one analyzer failing must not hide the rest
            out[key] = {"error": f"{type(exc).__name__}: {exc}"}
    out["limits"] = ("Static reading of the M text in the saved project. Folding, SQL and SharePoint findings are leads, not proof; "
                     "row counts and runtime behaviour need the live file (read_mashup_traces, audit_dashboard).")
    return out
