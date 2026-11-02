"""Read, validate and edit the PBIR report definition (<Report>/definition/**.json)."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
# Used only when a report has no sibling file to copy a $schema from. Desktop accepts older
# schema versions; these are the versions documented in Microsoft's json-schemas repository.
FALLBACK_SCHEMA = {
    "report": f"{SCHEMA_BASE}/report/3.0.0/schema.json",
    "page": f"{SCHEMA_BASE}/page/2.0.0/schema.json",
    "pages": f"{SCHEMA_BASE}/pagesMetadata/1.0.0/schema.json",
    "visual": f"{SCHEMA_BASE}/visualContainer/2.0.0/schema.json",
    "version": f"{SCHEMA_BASE}/versionMetadata/1.0.0/schema.json",
}

# Query-state role names by visual type (first = default role for an added field).
ROLES = {
    "card": ["Values"], "multiRowCard": ["Values"], "slicer": ["Values"], "tableEx": ["Values"], "pivotTable": ["Rows", "Columns", "Values"],
    "clusteredBarChart": ["Category", "Y", "Series"], "clusteredColumnChart": ["Category", "Y", "Series"],
    "stackedBarChart": ["Category", "Y", "Series"], "stackedColumnChart": ["Category", "Y", "Series"],
    "lineChart": ["Category", "Y", "Series"], "areaChart": ["Category", "Y", "Series"], "pieChart": ["Category", "Y"],
    "donutChart": ["Category", "Y"], "scatterChart": ["Category", "X", "Y", "Size"], "gauge": ["Y"], "kpi": ["Indicator", "TrendLine", "Goal"],
    "waterfallChart": ["Category", "Y"], "funnel": ["Category", "Y"], "treemap": ["Group", "Values"], "map": ["Category", "Size"],
    "textbox": [], "shape": [], "image": [], "actionButton": [],
}


class PbirError(ValueError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex[:20]


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


@dataclass
class Report:
    root: Path                                    # the definition folder
    files: dict[str, Any] = field(default_factory=dict)   # rel posix path -> parsed JSON
    errors: dict[str, str] = field(default_factory=dict)  # rel path -> parse error

    @classmethod
    def load(cls, definition_dir: Path) -> "Report":
        r = cls(root=definition_dir)
        for p in sorted(definition_dir.rglob("*.json")):
            rel = p.relative_to(definition_dir).as_posix()
            try:
                r.files[rel] = json.loads(p.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as exc:
                r.errors[rel] = str(exc)
        return r

    @classmethod
    def from_texts(cls, texts: dict[str, str]) -> "Report":
        r = cls(root=Path("."))
        for rel, text in texts.items():
            try:
                r.files[rel] = json.loads(text)
            except ValueError as exc:
                r.errors[rel] = str(exc)
        return r

    # ---------------------------------------------------------------- structure
    def page_ids(self) -> list[str]:
        ids = {rel.split("/")[1] for rel in list(self.files) + list(self.errors) if rel.startswith("pages/") and rel.count("/") >= 2}
        return sorted(ids)

    def page_order(self) -> list[str]:
        meta = self.files.get("pages/pages.json") or {}
        return list(meta.get("pageOrder") or [])

    def visual_ids(self, page_id: str) -> list[str]:
        pre = f"pages/{page_id}/visuals/"
        return sorted({rel[len(pre):].split("/")[0] for rel in list(self.files) + list(self.errors) if rel.startswith(pre)})

    def page(self, page_id: str) -> dict[str, Any]:
        return self.files.get(f"pages/{page_id}/page.json") or {}

    def visual(self, page_id: str, visual_id: str) -> dict[str, Any]:
        return self.files.get(f"pages/{page_id}/visuals/{visual_id}/visual.json") or {}

    def resolve_page(self, ref: str) -> str:
        """Accept a page id (folder name) or display name."""
        ids = self.page_ids()
        if ref in ids:
            return ref
        hits = [i for i in ids if (self.page(i).get("displayName") or "").lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]
        raise PbirError(f"Page {ref!r} not found or ambiguous. Pages: " + ", ".join(f"{i} ({self.page(i).get('displayName')})" for i in ids))

    def known_schema(self, kind: str) -> str:
        pat = {"report": "report.json", "page": "page.json", "pages": "pages.json", "visual": "visual.json", "version": "version.json"}[kind]
        for rel, data in self.files.items():
            if rel.endswith(pat) and isinstance(data, dict) and data.get("$schema"):
                return data["$schema"]
        return FALLBACK_SCHEMA[kind]

    # ---------------------------------------------------------------- summaries
    def summary(self) -> dict[str, Any]:
        pages = []
        order = self.page_order()
        for pid in sorted(self.page_ids(), key=lambda i: order.index(i) if i in order else 999):
            pg = self.page(pid)
            visuals = []
            for vid in self.visual_ids(pid):
                v = self.visual(pid, vid)
                visuals.append(visual_summary(vid, v))
            pages.append({
                "id": pid, "display_name": pg.get("displayName"), "width": pg.get("width"), "height": pg.get("height"),
                "hidden": pg.get("visibility") == "HiddenInViewMode", "display_option": pg.get("displayOption"),
                "page_type": (pg.get("type") or "Default"), "visual_count": len(visuals), "visuals": visuals,
                "filters": len((pg.get("filterConfig") or {}).get("filters") or []),
            })
        meta = self.files.get("pages/pages.json") or {}
        return {"page_count": len(pages), "active_page": meta.get("activePageName"), "pages": pages,
                "bookmark_count": len([r for r in self.files if r.endswith(".bookmark.json")]),
                "report_filters": len(((self.files.get("report.json") or {}).get("filterConfig") or {}).get("filters") or []),
                "theme": ((self.files.get("report.json") or {}).get("themeCollection") or {}),
                "parse_errors": self.errors}


def visual_summary(vid: str, v: dict[str, Any]) -> dict[str, Any]:
    vis = v.get("visual") or {}
    pos = v.get("position") or {}
    title = _title_text(vis)
    return {
        "id": vid, "type": vis.get("visualType") or ("group" if v.get("visualGroup") else None), "title": title,
        "x": pos.get("x"), "y": pos.get("y"), "width": pos.get("width"), "height": pos.get("height"), "z": pos.get("z"),
        "hidden": bool(v.get("isHidden")), "group": v.get("parentGroupName"),
        "alt_text": _alt_text(vis, v),
        "fields": [f"{k}:{t}[{n}]" for k, t, n in unique_fields(v)],
        "filters": len((v.get("filterConfig") or {}).get("filters") or []),
    }


def _literal(node: Any) -> str | None:
    try:
        raw = node["expr"]["Literal"]["Value"]
        return raw.strip("'") if isinstance(raw, str) else str(raw)
    except (KeyError, TypeError):
        return None


def _title_text(vis: dict[str, Any]) -> str | None:
    try:
        arr = vis["visualContainerObjects"]["title"]
        for item in arr:
            t = _literal((item.get("properties") or {}).get("text"))
            if t:
                return t
    except (KeyError, TypeError):
        pass
    return None


def _alt_text(vis: dict[str, Any], v: dict[str, Any]) -> str | None:
    try:
        for item in vis["visualContainerObjects"]["general"]:
            t = _literal((item.get("properties") or {}).get("altText"))
            if t:
                return t
    except (KeyError, TypeError):
        pass
    return None


# --------------------------------------------------------------------------------------
# Field references
# --------------------------------------------------------------------------------------

def iter_fields(node: Any, aliases: dict[str, str] | None = None) -> Iterator[tuple[str, str, str, str]]:
    """Yield (kind, table, name, path-hint) for every model field a JSON subtree references."""
    aliases = dict(aliases or {})
    if isinstance(node, dict):
        if isinstance(node.get("From"), list):
            for f in node["From"]:
                if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                    aliases[f["Name"]] = f["Entity"]
        for kind in ("Column", "Measure"):
            inner = node.get(kind)
            if isinstance(inner, dict) and "Property" in inner:
                ent = _entity(inner.get("Expression"), aliases)
                if ent:
                    yield kind, ent, inner["Property"], kind
        hl = node.get("HierarchyLevel")
        if isinstance(hl, dict) and "Level" in hl:
            hier = (hl.get("Expression") or {}).get("Hierarchy") or {}
            ent = _entity(hier.get("Expression"), aliases)
            if ent:
                yield "Hierarchy", ent, f"{hier.get('Hierarchy')}.{hl['Level']}", "HierarchyLevel"
        for v in node.values():
            yield from iter_fields(v, aliases)
    elif isinstance(node, list):
        for v in node:
            yield from iter_fields(v, aliases)


def _entity(expr: Any, aliases: dict[str, str]) -> str | None:
    if not isinstance(expr, dict):
        return None
    sr = expr.get("SourceRef")
    if not isinstance(sr, dict):
        return None
    if "Entity" in sr:
        return sr["Entity"]
    if "Source" in sr:
        return aliases.get(sr["Source"], sr["Source"])
    return None


def unique_fields(obj: Any) -> list[tuple[str, str, str]]:
    seen: dict[tuple[str, str, str], None] = {}
    for kind, table, name, _ in iter_fields(obj):
        seen[(kind, table, name)] = None
    return list(seen)


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------

def validate(report: Report, model_fields: dict[str, dict[str, set[str]]] | None = None,
             extension_entities: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    """Findings: {severity, rule, where, message}. `model_fields` maps lower(table) ->
    {"columns": {lower names}, "measures": {...}, "hierarchies": {...}} when the model is known."""
    out: list[dict[str, Any]] = []

    def add(sev: str, rule: str, where: str, msg: str) -> None:
        out.append({"severity": sev, "rule": rule, "where": where, "message": msg})

    for rel, err in report.errors.items():
        add("high", "PBIR001", rel, f"Invalid JSON: {err}")
    for rel, data in report.files.items():
        if isinstance(data, dict) and "$schema" not in data and not rel.endswith(".bookmark.json"):
            add("medium", "PBIR002", rel, "Missing $schema; editors and Desktop validate against it.")
    ids = report.page_ids()
    order = report.page_order()
    for pid in order:
        if pid not in ids:
            add("high", "PBIR010", "pages/pages.json", f"pageOrder lists {pid!r} but there is no pages/{pid} folder.")
    for pid in ids:
        if order and pid not in order:
            add("medium", "PBIR011", f"pages/{pid}", "Page folder is not listed in pageOrder (Desktop may drop or append it).")
    active = (report.files.get("pages/pages.json") or {}).get("activePageName")
    if active and active not in ids:
        add("high", "PBIR012", "pages/pages.json", f"activePageName {active!r} does not match any page folder.")
    for pid in ids:
        pg = report.page(pid)
        if not pg:
            add("high", "PBIR013", f"pages/{pid}", "Folder has no readable page.json.")
            continue
        if pg.get("name") != pid:
            add("high", "PBIR014", f"pages/{pid}/page.json", f"name {pg.get('name')!r} must equal the folder name {pid!r}.")
        if not pg.get("displayName"):
            add("medium", "PBIR015", f"pages/{pid}/page.json", "Page has no displayName.")
        pw, ph = pg.get("width") or 1280, pg.get("height") or 720
        vids = report.visual_ids(pid)
        if len(vids) > 20:
            add("info", "PBIR030", f"pages/{pid}", f"{len(vids)} visuals on one page; each issues its own query. Consider fewer, or split the page.")
        titles_seen: dict[str, str] = {}
        for vid in vids:
            v = report.visual(pid, vid)
            where = f"pages/{pid}/visuals/{vid}"
            if not v:
                add("high", "PBIR020", where, "Folder has no readable visual.json.")
                continue
            if v.get("name") != vid:
                add("high", "PBIR021", where, f"name {v.get('name')!r} must equal the folder name {vid!r}.")
            pos = v.get("position")
            if not pos:
                add("high", "PBIR022", where, "Visual has no position.")
            else:
                w, h, x, y = pos.get("width", 0), pos.get("height", 0), pos.get("x", 0), pos.get("y", 0)
                if w <= 0 or h <= 0:
                    add("high", "PBIR023", where, f"Non-positive size {w}x{h}.")
                if x < 0 or y < 0 or x + w > pw + 1 or y + h > ph + 1:
                    add("medium", "PBIR024", where, f"Visual extends outside the {pw}x{ph} page canvas.")
            vis = v.get("visual") or {}
            vt = vis.get("visualType")
            if v.get("visual") is not None and not vt:
                add("high", "PBIR025", where, "visual.visualType is missing.")
            if vt and vt not in ("textbox", "shape", "image", "actionButton") and not (_title_text(vis) or _alt_text(vis, v)) and not v.get("isHidden"):
                add("info", "PBIR040", where, f"{vt} has no title or alt text (accessibility).")
            if v.get("isHidden"):
                add("info", "PBIR041", where, "Visual is hidden; confirm it is intentional.")
            if model_fields is not None:
                for kind, table, name in unique_fields(v):
                    _check_field(add, where, kind, table, name, model_fields, extension_entities)
        pf = pg.get("filterConfig")
        if model_fields is not None and pf:
            for kind, table, name in unique_fields(pf):
                _check_field(add, f"pages/{pid}/page.json", kind, table, name, model_fields, extension_entities)
    rf = (report.files.get("report.json") or {}).get("filterConfig")
    if model_fields is not None and rf:
        for kind, table, name in unique_fields(rf):
            _check_field(add, "report.json", kind, table, name, model_fields, extension_entities)
    return out


def _check_field(add, where: str, kind: str, table: str, name: str, mf: dict[str, dict[str, set[str]]],
                 ext: dict[str, set[str]] | None) -> None:
    t = mf.get(table.lower())
    if ext and name.lower() in (ext.get(table.lower()) or ()):
        return
    if t is None:
        if ext and table.lower() in ext:
            return
        add("high", "PBIR050", where, f"References table {table!r} that is not in the semantic model ({kind} {name!r}).")
        return
    bucket = {"Column": "columns", "Measure": "measures", "Hierarchy": "hierarchies"}[kind]
    key = name.split(".")[0].lower() if kind == "Hierarchy" else name.lower()
    if key not in t[bucket]:
        other = [b for b in ("columns", "measures") if key in t[b]]
        hint = f" (a {other[0][:-1]} with that name exists; wrong field kind)" if other else ""
        add("high", "PBIR051", where, f"{kind} {table}[{name}] does not exist in the model{hint}.")


def model_field_index(model_dict: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    idx: dict[str, dict[str, set[str]]] = {}
    for t in model_dict["tables"]:
        idx[t["name"].lower()] = {
            "columns": {c["name"].lower() for c in t["columns"]},
            "measures": {m["name"].lower() for m in t["measures"]},
            "hierarchies": {h["name"].lower() for h in t["hierarchies"]},
        }
    return idx


def extension_index(report: Report) -> dict[str, set[str]]:
    ext = report.files.get("reportExtensions.json") or {}
    out: dict[str, set[str]] = {}
    for ent in ext.get("entities") or []:
        names = {m.get("name", "").lower() for m in ent.get("measures") or []}
        out[(ent.get("name") or "").lower()] = names
    return out


# --------------------------------------------------------------------------------------
# Edits: each returns {rel_path: new_text or None(delete)} so callers can diff / apply atomically.
# --------------------------------------------------------------------------------------

def field_ref(kind: str, table: str, name: str) -> dict[str, Any]:
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def literal(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}


def add_page(report: Report, display_name: str, width: int = 1280, height: int = 720, make_active: bool = False) -> tuple[str, dict[str, str | None]]:
    pid = new_id()
    page = {"$schema": report.known_schema("page"), "name": pid, "displayName": display_name, "displayOption": "FitToPage",
            "height": height, "width": width}
    meta = json.loads(json.dumps(report.files.get("pages/pages.json") or {"$schema": report.known_schema("pages")}))
    meta["pageOrder"] = list(meta.get("pageOrder") or report.page_ids()) + [pid]
    if make_active or not meta.get("activePageName"):
        meta["activePageName"] = pid
    return pid, {f"pages/{pid}/page.json": dumps(page), "pages/pages.json": dumps(meta)}


def add_visual(report: Report, page_ref: str, visual_type: str, fields: list[dict[str, str]], x: float, y: float,
               width: float, height: float, title: str | None = None) -> tuple[str, dict[str, str | None]]:
    pid = report.resolve_page(page_ref)
    if visual_type not in ROLES:
        raise PbirError(f"visual_type {visual_type!r} is not one of: {', '.join(sorted(ROLES))}")
    roles = ROLES[visual_type]
    if fields and not roles:
        raise PbirError(f"{visual_type} does not take data fields.")
    state: dict[str, Any] = {}
    for f in fields:
        kind = (f.get("kind") or "Column").capitalize()
        if kind not in ("Column", "Measure"):
            raise PbirError("field kind must be Column or Measure.")
        role = f.get("role") or roles[0]
        if role not in roles:
            raise PbirError(f"role {role!r} is not valid for {visual_type}; use one of {roles}.")
        proj = {"field": field_ref(kind, f["table"], f["name"]), "queryRef": f"{f['table']}.{f['name']}", "nativeQueryRef": f["name"]}
        state.setdefault(role, {"projections": []})["projections"].append(proj)
    vid = new_id()
    vis: dict[str, Any] = {"visualType": visual_type, "drillFilterOtherVisuals": True}
    if state:
        vis["query"] = {"queryState": state}
    if title:
        vis["visualContainerObjects"] = {"title": [{"properties": {"text": literal(title), "show": {"expr": {"Literal": {"Value": "true"}}}}}]}
    z = len(report.visual_ids(pid)) * 1000
    doc = {"$schema": report.known_schema("visual"), "name": vid,
           "position": {"x": x, "y": y, "z": z, "height": height, "width": width, "tabOrder": z}, "visual": vis}
    return vid, {f"pages/{pid}/visuals/{vid}/visual.json": dumps(doc)}


def update_visual(report: Report, page_ref: str, visual_id: str, *, title: str | None = None, alt_text: str | None = None,
                  x: float | None = None, y: float | None = None, width: float | None = None, height: float | None = None,
                  hidden: bool | None = None) -> dict[str, str | None]:
    pid = report.resolve_page(page_ref)
    rel = f"pages/{pid}/visuals/{visual_id}/visual.json"
    if rel not in report.files:
        raise PbirError(f"No visual {visual_id!r} on page {pid}.")
    v = json.loads(json.dumps(report.files[rel]))
    pos = v.setdefault("position", {})
    for key, val in (("x", x), ("y", y), ("width", width), ("height", height)):
        if val is not None:
            pos[key] = val
    if hidden is not None:
        if hidden:
            v["isHidden"] = True
        else:
            v.pop("isHidden", None)
    vis = v.setdefault("visual", {})
    if title is not None:
        vco = vis.setdefault("visualContainerObjects", {})
        vco["title"] = [{"properties": {"text": literal(title), "show": {"expr": {"Literal": {"Value": "true"}}}}}]
    if alt_text is not None:
        vco = vis.setdefault("visualContainerObjects", {})
        gen = vco.setdefault("general", [{"properties": {}}])
        gen[0].setdefault("properties", {})["altText"] = literal(alt_text)
    return {rel: dumps(v)}


def delete_visual(report: Report, page_ref: str, visual_id: str) -> dict[str, str | None]:
    pid = report.resolve_page(page_ref)
    pre = f"pages/{pid}/visuals/{visual_id}/"
    rels = [r for r in list(report.files) + list(report.errors) if r.startswith(pre)]
    if not rels:
        raise PbirError(f"No visual {visual_id!r} on page {pid}.")
    return {r: None for r in rels}


def delete_page(report: Report, page_ref: str) -> dict[str, str | None]:
    pid = report.resolve_page(page_ref)
    if len(report.page_ids()) <= 1:
        raise PbirError("A report must keep at least one page.")
    out: dict[str, str | None] = {r: None for r in list(report.files) + list(report.errors) if r.startswith(f"pages/{pid}/")}
    meta = json.loads(json.dumps(report.files.get("pages/pages.json") or {}))
    meta["pageOrder"] = [p for p in meta.get("pageOrder", []) if p != pid]
    if meta.get("activePageName") == pid:
        meta["activePageName"] = meta["pageOrder"][0] if meta["pageOrder"] else None
        if meta["activePageName"] is None:
            meta.pop("activePageName")
    out["pages/pages.json"] = dumps(meta)
    return out


def set_page(report: Report, page_ref: str, *, display_name: str | None = None, hidden: bool | None = None,
             width: int | None = None, height: int | None = None) -> dict[str, str | None]:
    pid = report.resolve_page(page_ref)
    rel = f"pages/{pid}/page.json"
    pg = json.loads(json.dumps(report.files[rel]))
    if display_name is not None:
        pg["displayName"] = display_name
    if hidden is not None:
        if hidden:
            pg["visibility"] = "HiddenInViewMode"
        else:
            pg.pop("visibility", None)
    if width is not None:
        pg["width"] = width
    if height is not None:
        pg["height"] = height
    return {rel: dumps(pg)}


def reorder_pages(report: Report, order: list[str]) -> dict[str, str | None]:
    ids = [report.resolve_page(p) for p in order]
    if sorted(ids) != sorted(report.page_ids()):
        raise PbirError("order must list every page exactly once.")
    meta = json.loads(json.dumps(report.files.get("pages/pages.json") or {"$schema": report.known_schema("pages")}))
    meta["pageOrder"] = ids
    return {"pages/pages.json": dumps(meta)}


def rename_field_refs(report: Report, kind: str, table: str | None, old: str, new: str, new_table: str | None = None) -> dict[str, str | None]:
    """Rewrite every reference to a renamed Column/Measure (kind) or table (kind='table') across the report."""
    out: dict[str, str | None] = {}
    for rel, data in report.files.items():
        copy = json.loads(json.dumps(data))
        n = _rename(copy, kind, table, old, new, {})
        # queryRef / nativeQueryRef strings
        n += _rename_query_refs(copy, kind, table, old, new)
        if n:
            out[rel] = dumps(copy)
    return out


def _rename(node: Any, kind: str, table: str | None, old: str, new: str, aliases: dict[str, str]) -> int:
    count = 0
    if isinstance(node, dict):
        aliases = dict(aliases)
        if isinstance(node.get("From"), list):
            for f in node["From"]:
                if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                    aliases[f["Name"]] = f["Entity"]
                    if kind == "table" and f["Entity"].lower() == old.lower():
                        f["Entity"] = new
                        count += 1
        if kind == "table":
            sr = node.get("SourceRef")
            if isinstance(sr, dict) and str(sr.get("Entity", "")).lower() == old.lower():
                sr["Entity"] = new
                count += 1
        else:
            for k in ("Column", "Measure"):
                inner = node.get(k)
                want = "Column" if kind == "column" else "Measure"
                if k == want and isinstance(inner, dict) and inner.get("Property") == old:
                    ent = _entity(inner.get("Expression"), aliases)
                    if ent and (table is None or ent.lower() == table.lower()):
                        inner["Property"] = new
                        count += 1
        for v in node.values():
            count += _rename(v, kind, table, old, new, aliases)
    elif isinstance(node, list):
        for v in node:
            count += _rename(v, kind, table, old, new, aliases)
    return count


def _rename_query_refs(node: Any, kind: str, table: str | None, old: str, new: str) -> int:
    """Rewrite queryRef / metadata strings (including aggregated forms like Sum(T.Old)) and the nativeQueryRef of renamed fields."""
    count = 0
    if isinstance(node, dict):
        for key in ("queryRef", "metadata"):
            val = node.get(key)
            if not isinstance(val, str):
                continue
            if kind == "table":
                pat, repl = rf"(?<![\w.]){re.escape(old)}\.", new + "."
            elif table:
                pat, repl = rf"(?<![\w.]){re.escape(table)}\.{re.escape(old)}(?!\w)", f"{table}.{new}"
            else:
                continue
            nv = re.sub(pat, lambda _m, r=repl: r, val, flags=re.I)
            if nv != val:
                node[key] = nv
                count += 1
        if kind != "table" and table and isinstance(node.get("field"), dict) and node.get("nativeQueryRef") == old:
            if any(t.lower() == table.lower() and n == new for _k, t, n in unique_fields(node["field"])):
                node["nativeQueryRef"] = new
                count += 1
        for v in node.values():
            count += _rename_query_refs(v, kind, table, old, new)
    elif isinstance(node, list):
        for v in node:
            count += _rename_query_refs(v, kind, table, old, new)
    return count


def apply_theme_edit(report: Report, theme_name: str, theme_json: dict[str, Any]) -> dict[str, str | None]:
    """Register `theme_json` as the report's custom theme (StaticResources/RegisteredResources)."""
    rep = json.loads(json.dumps(report.files.get("report.json") or {}))
    safe = re.sub(r"[^\w\-]+", "_", theme_name)
    file_name = f"{safe}.json"
    tc = rep.setdefault("themeCollection", {})
    tc["customTheme"] = {"name": file_name, "reportVersionAtImport": (tc.get("baseTheme") or {}).get("reportVersionAtImport", "5.59"),
                         "type": "RegisteredResources"}
    res = rep.setdefault("resourcePackages", [])
    pkg = next((p for p in res if p.get("name") == "RegisteredResources"), None)
    if pkg is None:
        pkg = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
        res.append(pkg)
    pkg["items"] = [i for i in pkg.get("items", []) if i.get("name") != file_name] + [
        {"name": file_name, "path": file_name, "type": "CustomTheme"}]
    return {"report.json": dumps(rep), f"../StaticResources/RegisteredResources/{file_name}": dumps(theme_json)}
