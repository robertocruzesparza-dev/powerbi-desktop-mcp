"""Structural validity checks for a whole project (the gate every edit must pass)."""
from __future__ import annotations

import json
from typing import Any

from . import daxrefs, pbir, tmdl
from .model import Model
from .project import Project


def _add(out: list[dict[str, Any]], sev: str, rule: str, where: str, msg: str) -> None:
    out.append({"severity": sev, "rule": rule, "where": where, "message": msg})


def validate_model(model: Model) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rel, err in model.parse_errors.items():
        _add(out, "high", "TMDL001", rel, f"Cannot parse: {err}")
    for rel, doc in model.docs.items():
        for problem in tmdl.check(doc):
            _add(out, "high" if "duplicate" in problem else "medium", "TMDL002", rel, problem)
    tables = {t.node.name.lower(): t.node for t in model.tables()}
    seen_tables: dict[str, str] = {}
    for t in model.tables():
        key = t.node.name.lower()
        if key in seen_tables:
            _add(out, "high", "TMDL003", t.file, f"Table {t.node.name!r} is also defined in {seen_tables[key]}.")
        seen_tables[key] = t.file
    measure_names: dict[str, str] = {}
    for tname, loc in model.measures():
        key = loc.node.name.lower()
        if key in measure_names and measure_names[key] != tname:
            _add(out, "high", "TMDL004", loc.file, f"Measure {loc.node.name!r} exists in both {measure_names[key]!r} and {tname!r}; measure names must be unique model-wide.")
        measure_names[key] = tname
        col_clash = tables.get(tname.lower())
        if col_clash is not None and col_clash.child_ci("column", loc.node.name):
            _add(out, "high", "TMDL005", loc.file, f"Measure {tname}[{loc.node.name}] has the same name as a column in that table.")
        if not (loc.node.expr or "").strip():
            _add(out, "high", "TMDL006", loc.file, f"Measure {tname}[{loc.node.name}] has no expression.")
        for problem in daxrefs.balanced(loc.node.expr or ""):
            _add(out, "high", "DAX100", loc.file, f"{tname}[{loc.node.name}]: {problem}.")
    for rel in model.relationships():
        for side in ("from", "to"):
            t, c = rel[f"{side}_table"], rel[f"{side}_column"]
            tn = tables.get((t or "").lower())
            if tn is None:
                _add(out, "high", "TMDL010", rel["file"], f"Relationship {rel['id']} {side} table {t!r} does not exist.")
            elif not tn.child("column", c or ""):
                _add(out, "high", "TMDL011", rel["file"], f"Relationship {rel['id']} {side} column {t}[{c}] does not exist.")
    for tname, loc in model.columns():
        sbc = loc.node.prop("sortByColumn")
        if sbc:
            tn = tables.get(tname.lower())
            if tn is not None and not tn.child("column", tmdl.unquote_name(sbc)):
                _add(out, "high", "TMDL012", loc.file, f"{tname}[{loc.node.name}] sorts by missing column {sbc!r}.")
    graph = model.measure_graph()
    for cyc in graph["cycles"]:
        _add(out, "high", "DAX101", "model", "Circular measure reference: " + " -> ".join(cyc))
    for u in model.unresolved_dax():
        if u["qualified"]:
            _add(out, "high", "DAX103", u["in"], f"Reference {u['ref']} is broken: {u['why']}.")
        else:
            _add(out, "medium", "DAX102", u["in"], f"Reference {u['ref']} does not resolve to any measure or column in the model.")
    for t in model.tables():
        for h in t.node.kids("hierarchy"):
            for lv in h.kids("level"):
                col = lv.prop("column")
                if col and t.node.child_ci("column", tmdl.unquote_name(col)) is None:
                    _add(out, "high", "TMDL013", t.file, f"Hierarchy {t.node.name}.{h.name} level {lv.name!r} uses missing column {col!r}.")
    return out


def validate_report(project: Project, model: Model | None) -> list[dict[str, Any]]:
    if project.pbir_dir is None:
        return []
    rep = pbir.Report.load(project.pbir_dir)
    mf = pbir.model_field_index(model.to_dict(include_expressions=False)) if model is not None and model.docs else None
    return pbir.validate(rep, mf, pbir.extension_index(rep))


def validate_project(project: Project) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    model = None
    if project.tmdl_dir:
        model = Model.load(project.tmdl_dir)
        findings += [{**f, "layer": "model"} for f in validate_model(model)]
    elif project.model_dir is not None:
        _add(findings, "info", "PRJ001", str(project.model_dir), "Semantic model is not in TMDL (model.bim or missing); model checks skipped.")
    if project.model_dir is not None:
        pbism = project.model_dir / "definition.pbism"
        if not pbism.is_file():
            _add(findings, "medium", "PRJ002", str(pbism), "definition.pbism is missing; Desktop recreates it on save but external tools expect it.")
    if project.report_dir is not None:
        pbir_file = project.report_dir / "definition.pbir"
        if not pbir_file.is_file():
            _add(findings, "high", "PRJ003", str(pbir_file), "definition.pbir is missing (required).")
        else:
            try:
                json.loads(pbir_file.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                _add(findings, "high", "PRJ004", str(pbir_file), f"definition.pbir is not valid JSON: {exc}")
        if project.report_format == "pbir":
            findings += [{**f, "layer": "report"} for f in validate_report(project, model)]
        elif project.report_format == "pbir-legacy":
            _add(findings, "info", "PRJ005", str(project.report_dir), "Report uses the legacy report.json format, which Microsoft does not support for external editing; report checks skipped.")
    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f.get("layer", ""), f["where"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "info")}
    return {"ok": counts["high"] == 0, "counts": counts, "findings": findings}


BLOCKING_MEDIUM = frozenset({"DAX102"})


def is_blocking(f: dict[str, Any]) -> bool:
    """Findings an edit must not ADD: every high one, plus new unresolved DAX references."""
    return f["severity"] == "high" or f["rule"] in BLOCKING_MEDIUM


def high_keys(result: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(f["rule"], f["where"], f["message"]) for f in result["findings"] if is_blocking(f)}
