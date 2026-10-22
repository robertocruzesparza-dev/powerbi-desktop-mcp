"""Compare two dashboards: model (tables, columns, measures + DAX, relationships,
RLS, Power Query steps) and report layout (pages, visuals, filters).

Each side is either an OPEN file (by the name/port from list_open_files: full
model available) or a .pbix path. Only the report layout is readable from a
closed .pbix -- the data model lives in a compressed stream that needs the
Analysis Services engine -- so a closed file gets a layout comparison plus,
when the file still embeds a DataMashup stream (older format; 1 of 41 tested
files did), its Power Query code.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from . import layout, metadata
from .discovery import PbiInstance
from .engine import list_instances

_CAP = 40


# ----------------------------------------------------------------------------
# Offline Power Query extraction (legacy pbix with a DataMashup stream)
# ----------------------------------------------------------------------------

def extract_mashup_queries(pbix_path: str | Path) -> dict[str, str] | None:
    """{query name: M text} from a .pbix's DataMashup stream, or None if the
    file has no such stream (newer files keep M inside the compressed model)."""
    with zipfile.ZipFile(pbix_path) as zf:
        if "DataMashup" not in zf.namelist():
            return None
        raw = zf.read("DataMashup")
    plen = int.from_bytes(raw[4:8], "little")
    try:
        with zipfile.ZipFile(io.BytesIO(raw[8:8 + plen])) as pz:
            text = pz.read("Formulas/Section1.m").decode("utf-8-sig")
    except (KeyError, zipfile.BadZipFile):
        return None
    starts = list(re.finditer(r'^shared\s+(#"(?:[^"]|"")+"|\w+)\s*=\s*', text, re.M))
    out: dict[str, str] = {}
    for i, m in enumerate(starts):
        name = m.group(1)
        name = name[2:-1].replace('""', '"') if name.startswith('#"') else name
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        out[name] = text[m.end():end].strip().rstrip(";").strip()
    return out


# ----------------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    t = re.sub(r"//[^\n]*", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def snapshot_live(inst: PbiInstance) -> dict[str, Any]:
    tables = metadata.list_tables(inst)["rows"]
    cols = metadata.list_columns(inst)["rows"]
    meas = metadata.list_measures(inst)["rows"]
    rels = metadata.list_relationships(inst)["rows"]
    roles = metadata.list_security_roles(inst)["roles"]
    pq = metadata.list_power_query(inst, include_steps=False)
    queries = {t["table"]: t["m_query"] for t in pq["table_queries"] if t.get("is_m")}
    queries.update({s["name"]: s["m_query"] for s in pq["shared_queries_and_parameters"] if not metadata._is_diagnostics_name(s["name"])})
    queries = {k: v for k, v in queries.items() if not metadata._is_diagnostics_name(k)}
    return {
        "tables": {t["Name"]: {"hidden": bool(t.get("IsHidden")), "dax": t.get("Expression")} for t in tables if not metadata._is_diagnostics_name(t.get("Name"))},
        "columns": {f"{c['Table']}.{c['Name']}": {"type": c.get("DataType"), "dax": c.get("Expression")} for c in cols if not str(c.get("Name", "")).startswith("RowNumber-")
                    and not metadata._is_diagnostics_name(c.get("Table"))},
        "measures": {f"{m['Table']}.{m['Name']}": _norm(m.get("Expression")) for m in meas},
        "relationships": {f"{r['FromTable']}[{r['FromColumn']}] -> {r['ToTable']}[{r['ToColumn']}]":
                          f"{r.get('FromCardinality')}:{r.get('ToCardinality')} active={r.get('IsActive')} filter={r.get('CrossFilteringBehavior')}" for r in rels},
        "roles": {r["role"]: {tp["table"]: _norm(tp["filter_expression"]) for tp in r["table_permissions"]} for r in roles},
        "queries": {k: _norm(v) for k, v in queries.items()},
    }


def _resolve(ident: str) -> dict[str, Any]:
    for inst in list_instances():
        if inst.display_name == ident or str(inst.port) == ident:
            return {"mode": "live", "instance": inst, "path": inst.file_path, "label": inst.display_name}
    p = Path(ident)
    if p.exists() and p.suffix.lower() in (".pbix", ".pbit"):
        return {"mode": "file", "instance": None, "path": str(p), "label": p.stem}
    raise ValueError(f"{ident!r} is neither an open Power BI file (see list_open_files) nor an existing .pbix path.")


# ----------------------------------------------------------------------------
# Diffing
# ----------------------------------------------------------------------------

def _cap(items: list[Any]) -> dict[str, Any]:
    return {"count": len(items), "items": items[:_CAP], **({"truncated": True} if len(items) > _CAP else {})}


def _diff_map(a: dict[str, Any], b: dict[str, Any], show=lambda v: v) -> dict[str, Any]:
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = [{"name": k, "left": show(a[k]), "right": show(b[k])} for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return {"only_in_right": _cap(added), "only_in_left": _cap(removed), "changed": _cap(changed)}


def _short(v: Any) -> Any:
    return v[:240] + "..." if isinstance(v, str) and len(v) > 240 else v


def _diff_queries(a: dict[str, str], b: dict[str, str]) -> dict[str, Any]:
    from . import mquery

    d = _diff_map({k: _norm(v) for k, v in a.items()}, {k: _norm(v) for k, v in b.items()}, _short)
    detailed = []
    for item in d["changed"]["items"]:
        pa, pb = mquery.parse_m(a[item["name"]]), mquery.parse_m(b[item["name"]])
        if pa.get("parsed") and pb.get("parsed"):
            sa = {s["name"]: _norm(s["formula"]) for s in pa["steps"] if s.get("name")}
            sb = {s["name"]: _norm(s["formula"]) for s in pb["steps"] if s.get("name")}
            item["steps"] = {"added": sorted(set(sb) - set(sa)), "removed": sorted(set(sa) - set(sb)),
                             "changed": sorted(k for k in set(sa) & set(sb) if sa[k] != sb[k])}
        detailed.append(item)
    d["changed"]["items"] = detailed
    return d


def _fsig(filters: list[dict[str, Any]]) -> list[tuple]:
    return sorted((tuple(f.get("field_refs") or f.get("fields") or ()), f.get("type"), tuple(f.get("values") or ()), bool(f.get("contains_negation"))) for f in filters or [])


def _diff_layout(pa: str, pb: str) -> dict[str, Any]:
    la, lb = layout.parse_layout(pa), layout.parse_layout(pb)
    pages_a = {p["display_name"]: p for p in la["pages"]}
    pages_b = {p["display_name"]: p for p in lb["pages"]}
    changed_pages = []
    for name in sorted(set(pages_a) & set(pages_b)):
        A, B = pages_a[name], pages_b[name]
        sig = lambda p: Counter((v.get("visual_type"), v.get("title"), tuple(v.get("field_refs") or ())) for v in p["visuals"] if "error" not in v)
        sa, sb = sig(A), sig(B)
        add, rem = sb - sa, sa - sb
        pf = _fsig(A["filters"]) != _fsig(B["filters"])
        vf_a = sum(len(v.get("filters") or []) for v in A["visuals"])
        vf_b = sum(len(v.get("filters") or []) for v in B["visuals"])
        if add or rem or pf or vf_a != vf_b or A.get("hidden") != B.get("hidden"):
            changed_pages.append({"page": name, "visuals_added": [f"{k[0]}: {k[1] or ''} {list(k[2])[:4]}" for k in add],
                                  "visuals_removed": [f"{k[0]}: {k[1] or ''} {list(k[2])[:4]}" for k in rem],
                                  "page_filters_changed": pf, "visual_filter_count": [vf_a, vf_b],
                                  "hidden": [A.get("hidden"), B.get("hidden")]})
    return {
        "pages_only_in_right": _cap(sorted(set(pages_b) - set(pages_a))),
        "pages_only_in_left": _cap(sorted(set(pages_a) - set(pages_b))),
        "pages_changed": _cap(changed_pages),
        "report_filters_changed": _fsig(la["report_filters"]) != _fsig(lb["report_filters"]),
        "formats": [la["format"], lb["format"]],
    }


def compare_dashboards(left: str, right: str) -> dict[str, Any]:
    L, R = _resolve(left), _resolve(right)
    out: dict[str, Any] = {"left": {"label": L["label"], "mode": L["mode"], "path": L["path"]},
                           "right": {"label": R["label"], "mode": R["mode"], "path": R["path"]}, "notes": []}
    if L["mode"] == "live" and R["mode"] == "live":
        a, b = snapshot_live(L["instance"]), snapshot_live(R["instance"])
        out["model"] = {
            "tables": _diff_map(a["tables"], b["tables"], lambda v: {k: _short(x) for k, x in v.items() if x}),
            "columns": _diff_map(a["columns"], b["columns"]),
            "measures": _diff_map(a["measures"], b["measures"], _short),
            "relationships": _diff_map(a["relationships"], b["relationships"]),
            "security_roles": _diff_map(a["roles"], b["roles"]),
            "power_query": _diff_queries({k: v for k, v in a["queries"].items()}, {k: v for k, v in b["queries"].items()}),
        }
    else:
        out["notes"].append("Data model comparison needs BOTH files open in Power BI Desktop (the model is only readable through its running engine). Open the missing file and call again.")
        qa = extract_mashup_queries(L["path"]) if L["path"] else None
        qb = extract_mashup_queries(R["path"]) if R["path"] else None
        if qa is not None and qb is not None:
            out["model"] = {"power_query": _diff_queries(qa, qb)}
            out["notes"].append("Power Query compared from the DataMashup stream embedded in both files.")
        else:
            out["notes"].append("Power Query can't be read offline from at least one file (newer .pbix files keep it inside the compressed model).")
    if L["path"] and R["path"]:
        try:
            out["report"] = _diff_layout(L["path"], R["path"])
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"Report layout comparison failed: {exc}")
    else:
        out["notes"].append("Report layout comparison needs a file path for both sides.")

    def total(node: Any) -> int:
        if isinstance(node, dict):
            return (node["count"] if "count" in node and "items" in node else 0) + sum(total(v) for v in node.values() if isinstance(v, (dict, list)))
        if isinstance(node, list):
            return sum(total(v) for v in node)
        return 0

    out["difference_count"] = total(out.get("model", {})) + total(out.get("report", {})) + int(bool(out.get("report", {}).get("report_filters_changed")))
    out["identical"] = out["difference_count"] == 0 and "model" in out and "report" in out
    return out
