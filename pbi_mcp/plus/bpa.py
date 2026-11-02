"""Best-practice analyzer for a TMDL semantic model (offline, no Desktop needed).

Findings are leads with a reason, not proof: they come from the model definition text, not from
engine statistics. Severity: high = likely wrong or slow, medium = worth fixing, info = hygiene.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from . import daxrefs
from .model import Model

_MONTH_WORDS = re.compile(r"(month\s*name|monthname|day\s*name|dayname|weekday\s*name|quarter\s*name)", re.I)
_PCT_NAME = re.compile(r"(%|\bpct\b|percent|\bmargin\b|\brate\b|\bratio\b|\bshare\b)", re.I)
_KEY_NAME = re.compile(r"(^id$|id$|key$|_id$|_key$|\bcode$)", re.I)
_AUTO_DATE = re.compile(r"^(LocalDateTable|DateTableTemplate)_", re.I)


def run(model: Model, rules: set[str] | None = None) -> dict[str, Any]:
    out: list[dict[str, Any]] = []

    def add(rule: str, sev: str, cat: str, where: str, msg: str, fix: str = "") -> None:
        if rules and rule not in rules:
            return
        out.append({"rule": rule, "severity": sev, "category": cat, "where": where, "message": msg, "fix": fix})

    d = model.to_dict()
    rels = d["relationships"]
    tables = {t["name"]: t for t in d["tables"]}
    many_side_cols = {(r["from_table"], r["from_column"]) for r in rels}
    key_cols = many_side_cols | {(r["to_table"], r["to_column"]) for r in rels}
    related_tables = {r["from_table"] for r in rels} | {r["to_table"] for r in rels}

    for r in rels:
        label = f"{r['from_table']}[{r['from_column']}] -> {r['to_table']}[{r['to_column']}]"
        if r["cross_filter"] == "bothDirections":
            add("BPA001", "medium", "performance", label, "Bidirectional relationship: ambiguous filter paths and slower queries.",
                "Use a single direction and CROSSFILTER/TREATAS inside the measures that need it.")
        if r["from_cardinality"] == "many" and r["to_cardinality"] == "many":
            add("BPA002", "medium", "model", label, "Many-to-many relationship.", "Introduce a bridge table or a shared dimension.")
        if not r["active"]:
            add("BPA036", "info", "model", label, "Inactive relationship: only used where a measure calls USERELATIONSHIP.", "Confirm a measure uses it or delete it.")

    for tname, t in tables.items():
        if _AUTO_DATE.match(tname):
            add("BPA033", "medium", "performance", tname, "Auto date/time table: Power BI created a hidden date table per date column.",
                "File > Options > Current file > Data load > turn off Auto date/time, and use one shared date table.")
        measures, columns = t["measures"], t["columns"]
        if not t["hidden"] and tname not in related_tables and not t["calculated"] and columns and not measures and not _AUTO_DATE.match(tname):
            add("BPA015", "info", "model", tname, "Visible table with no relationships and no measures (isolated).", "Relate it, hide it, or remove it.")
        for c in columns:
            where = f"{tname}[{c['name']}]"
            if (tname, c["name"]) in many_side_cols and not c["hidden"]:
                add("BPA003", "info", "usability", where, "Foreign-key column is visible to report authors.", "Set isHidden on the key column.")
            numeric = (c["data_type"] or "").lower() in ("int64", "double", "decimal")
            if numeric and (tname, c["name"]) in key_cols and (c["summarize_by"] or "default") not in ("none",):
                add("BPA004", "medium", "usability", where, "Numeric relationship key can be summed by accident.", "Set summarizeBy: none.")
            elif numeric and _KEY_NAME.search(c["name"]) and (c["summarize_by"] or "default") not in ("none",) and not c["hidden"]:
                add("BPA004", "info", "usability", where, "Numeric ID-like column is summarizable.", "Set summarizeBy: none.")
            if _MONTH_WORDS.search(c["name"]) and not c["sort_by"]:
                add("BPA029", "medium", "usability", where, "Month/day name column sorts alphabetically.", "Set sortByColumn to the matching number column.")
            if c["name"] != c["name"].strip() or "  " in c["name"]:
                add("BPA014", "medium", "naming", where, "Name has leading/trailing or doubled spaces.", "Rename it.")
            if c["calculated"] and c["expression"] and re.fullmatch(r"\s*(SUM|AVERAGE|COUNT|MIN|MAX)\w*\(.*\)\s*", c["expression"], re.S):
                add("BPA005", "medium", "performance", where, "Calculated column is a plain aggregation; it stores a constant per row.", "Make it a measure.")
        for m in measures:
            where = f"{tname}[{m['name']}]"
            if not m["format"]:
                add("BPA007", "info", "usability", where, "Measure has no format string.", "Set formatString.")
            if _PCT_NAME.search(m["name"]) and m["format"] and "%" not in m["format"]:
                add("BPA025", "info", "usability", where, "Name suggests a percentage but the format has no %.", "Use a 0.0% format.")
            if m["name"] != m["name"].strip() or "  " in m["name"]:
                add("BPA014", "medium", "naming", where, "Name has leading/trailing or doubled spaces.", "Rename it.")
            for lint in daxrefs.lint(m["expression"]):
                add(f"BPA-{lint.rule}", lint.severity, "dax", where, lint.message)
        if len(measures) > 15 and sum(1 for m in measures if m["display_folder"]) == 0:
            add("BPA009", "info", "usability", tname, f"{len(measures)} measures and no display folders.", "Group measures with displayFolder.")
        for c in columns:
            if c["calculated"] and c["expression"]:
                for lint in daxrefs.lint(c["expression"]):
                    add(f"BPA-{lint.rule}", lint.severity, "dax", f"{tname}[{c['name']}]", lint.message)

    names = Counter(m["name"].lower() for t in d["tables"] for m in t["measures"])
    for n, cnt in names.items():
        if cnt > 1:
            add("BPA037", "high", "model", n, f"Measure name {n!r} is defined {cnt} times.")

    date_tables = [t for t in d["tables"] if (t["data_category"] or "").lower() == "time" or
                   any((c["data_category"] or "").lower() in ("years", "months") for c in t["columns"])]
    has_date_cols = any((c["data_type"] or "").lower() == "datetime" for t in d["tables"] for c in t["columns"]
                        if not _AUTO_DATE.match(t["name"]))
    if has_date_cols and not date_tables and not any(_AUTO_DATE.match(t["name"]) for t in d["tables"]):
        add("BPA006", "medium", "model", "model", "Date columns exist but no table is marked as a date table.",
            "Add a Calendar table, mark it as a date table, and relate it to the fact tables.")

    graph = model.measure_graph()
    for tname, loc in model.measures():
        key = f"{tname}[{loc.node.name}]"
        if loc.node.flag("isHidden") and key not in graph["referenced_measures"]:
            add("BPA026", "info", "model", key, "Hidden measure that no other measure uses.", "Delete it unless a visual uses it.")
    for cyc in graph["cycles"]:
        add("BPA038", "high", "dax", "model", "Circular measure dependency: " + " -> ".join(cyc))
    for u in graph["unresolved_references"]:
        add("BPA039", "medium", "dax", u["in"], f"Unresolved reference {u['ref']}.")
    for tname, loc in model.measures():
        for r in daxrefs.refs(loc.node.expr or ""):
            if r.table is None:
                col = tables.get(tname, {"columns": []})["columns"]
                if any(c["name"].lower() == r.name.lower() for c in col) and r.name.lower() not in names:
                    add("BPA034", "info", "dax", f"{tname}[{loc.node.name}]", f"Unqualified column reference [{r.name}]; qualify columns as Table[Column] so they are not mistaken for measures.")
                    break

    for role in d["roles"]:
        if not role["permissions"]:
            add("BPA019", "medium", "security", f"role {role['name']}", "Role has no table filters: members see everything.", "Add a filter or delete the role.")
        for p in role["permissions"]:
            if not (p["filter"] or "").strip():
                add("BPA019", "medium", "security", f"role {role['name']} / {p['table']}", "Empty table filter.")
    for t in d["tables"]:
        for p in t["partitions"]:
            if (p.get("mode") or "").lower() == "directquery":
                add("BPA022", "info", "performance", t["name"], "DirectQuery partition: each visual query hits the source.", "Confirm it is needed; consider Import or aggregations.")
    db = _db_level(model)
    if db is not None and db < 1567:
        add("BPA030", "info", "model", "database.tmdl", f"compatibilityLevel {db} is older than 1567; newer modeling features may be unavailable.")

    order = {"high": 0, "medium": 1, "info": 2}
    out.sort(key=lambda f: (order[f["severity"]], f["category"], f["where"]))
    counts = {s: sum(1 for f in out if f["severity"] == s) for s in order}
    by_rule = Counter(f["rule"] for f in out)
    return {"summary": counts, "by_rule": dict(by_rule), "findings": out,
            "note": "Static analysis of the model definition. Findings are leads with a reason; nothing here measures runtime performance."}


def _db_level(model: Model) -> int | None:
    doc = getattr(model, "docs", {}).get("database.tmdl")
    if not doc:
        return None
    for n in doc.nodes:
        if n.kind == "database":
            v = n.prop("compatibilityLevel")
            if v and v.isdigit():
                return int(v)
    return None
