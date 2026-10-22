"""Offline parsing of a .pbix file's report layout (pages/visuals/fields),
plus a convenience scanner for finding .pbix files on disk.

This does NOT touch the data model (tables/measures/DAX) -- that lives in a
compressed proprietary stream inside the .pbix and can only be read
reliably by querying the live Analysis Services engine Power BI Desktop
runs while the file is open (see engine.py / metadata.py). This module only
covers the report layout, which Power BI stores as plain JSON inside the
zip and can be read with the file closed.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any


def _decode_layout_bytes(raw: bytes) -> str:
    for encoding in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode Report/Layout with any known encoding.")


def _maybe_json(value: Any) -> Any:
    """Layout JSON nests further JSON as strings in several places
    (per-page config, per-visual config, filters). Parse those through.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _find_all(obj: Any, key: str) -> list[Any]:
    """Recursively collect every value found under `key` anywhere in a
    nested dict/list structure. Used to pull field references (queryRef)
    out of visual configs without depending on the exact shape of a
    particular visual type's schema.
    """
    found: list[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key and isinstance(v, (str, int, float)):
                    found.append(v)
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found


def _field_refs(obj: Any) -> set[str]:
    """Every model field a chunk of report JSON refers to, as "Entity.Property".

    queryRef strings only cover fields dragged into a visual's wells. Fields
    also appear in conditional formatting, titles, tooltips, filters and
    sort orders, always as {Expression:{SourceRef:{Entity|Source}}, Property}.
    Filters and legacy queries alias entities (`From: [{Name:"t", Entity:"X"}]`
    then `SourceRef:{Source:"t"}`), so aliases are resolved per scope --
    the same alias letter can mean different tables in different filters.
    """
    refs: set[str] = set()

    def walk(node: Any, aliases: dict[str, str]) -> None:
        if isinstance(node, dict):
            local = aliases
            frm = node.get("From")
            if isinstance(frm, list):
                local = dict(aliases)
                for f in frm:
                    if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                        local[f["Name"]] = f["Entity"]
            expr = node.get("Expression")
            sref = expr.get("SourceRef") if isinstance(expr, dict) else None
            if isinstance(sref, dict):
                ent = sref.get("Entity") or local.get(sref.get("Source"))
                prop = node.get("Property")
                if ent and isinstance(prop, str):
                    refs.add(f"{ent}.{prop}")
            hier = node.get("Hierarchy")
            if isinstance(hier, dict):  # {"Hierarchy": {"Expression": {"SourceRef": ...}, "Hierarchy": "Date Hierarchy"}}
                hs = (hier.get("Expression") or {}).get("SourceRef") if isinstance(hier.get("Expression"), dict) else None
                if isinstance(hs, dict) and isinstance(hier.get("Hierarchy"), str):
                    ent = hs.get("Entity") or local.get(hs.get("Source"))
                    if ent:
                        refs.add(f"{ent}.{hier['Hierarchy']}")
            for v in node.values():
                walk(v, local)
        elif isinstance(node, list):
            for item in node:
                walk(item, aliases)

    walk(obj, {})
    return refs


def _contains_key(obj: Any, key: str) -> bool:
    """Recursively check whether `key` appears anywhere in a nested
    dict/list structure, regardless of the value at that key."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(item, key) for item in obj)
    return False


def _extract_filters(filter_config: Any) -> list[dict[str, Any]]:
    """Summarize the filters in a report/page/visual's filterConfig (PBIR)
    or filters blob (legacy, already run through _maybe_json). This is a
    hidden-data risk area: a filter set at any of these three levels
    silently excludes rows from everything downstream of it, and it's easy
    to overlook one buried in a specific visual while a page or report
    filter (usually invisible in the report's rendered surface) does the
    real damage. Rather than trying to fully decode every filter type's
    condition tree (Categorical/Advanced/TopN/RelativeDate all differ),
    this pulls out what's reliably extractable regardless of type: which
    fields it targets, what literal values it compares against, and
    whether its condition tree contains a negation anywhere (a rough but
    useful proxy for "this excludes rather than includes").
    """
    filter_config = _maybe_json(filter_config)
    if not isinstance(filter_config, dict):
        return []

    results = []
    for f in filter_config.get("filters", []) or []:
        try:
            fields = sorted(set(str(v) for v in _find_all(f.get("field", {}), "Property")))
            values = sorted(set(str(v) for v in _find_all(f.get("filter", {}), "Value")))
            results.append(
                {
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "fields": fields,
                    "values": values,
                    "field_refs": sorted(_field_refs(f)),
                    "contains_negation": _contains_key(f.get("filter", {}), "Not"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"error": f"could not parse filter: {exc}"})
    return results


def _extract_title(objects_root: dict) -> str | None:
    """Pull the visible title text out of a visual's container-level
    formatting objects. The property path is identical in shape between the
    legacy ('vcObjects') and PBIR ('visualContainerObjects') formats -- only
    the root key name differs.
    """
    try:
        title_props = objects_root.get("title", [{}])[0].get("properties", {})
        title_expr = title_props.get("text", {}).get("expr", {}).get("Literal", {}).get("Value")
        if title_expr:
            return title_expr.strip("'")
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return None


def _parse_visual_legacy(container: dict) -> dict[str, Any]:
    """Parse one visualContainer entry from the old single-blob
    Report/Layout format (pre-2024 default): visual config is JSON-encoded
    as a *string* inside the "config" field.
    """
    config = _maybe_json(container.get("config", "{}")) or {}
    single_visual = config.get("singleVisual", {}) if isinstance(config, dict) else {}
    visual_type = single_visual.get("visualType") if isinstance(single_visual, dict) else None
    fields = sorted(set(str(f) for f in _find_all(single_visual, "queryRef")))
    vc_objects = single_visual.get("vcObjects", {}) if isinstance(single_visual, dict) else {}

    display = single_visual.get("display") if isinstance(single_visual, dict) else None
    return {
        "visual_type": visual_type,
        "title": _extract_title(vc_objects),
        "fields": fields,
        "field_refs": sorted(_field_refs(config)),
        # Legacy hidden-visual storage (display.mode == "hidden") is per the
        # format docs; none of the 33 legacy files tested contained a hidden
        # visual, so this path is unverified against real data.
        "hidden": isinstance(display, dict) and str(display.get("mode", "")).lower() == "hidden",
        "filters": _extract_filters(container.get("filters")),
        "x": container.get("x"),
        "y": container.get("y"),
        "width": container.get("width"),
        "height": container.get("height"),
    }


def _parse_visual_pbir(visual_doc: dict) -> dict[str, Any]:
    """Parse one visuals/<id>/visual.json entry from the newer PBIR format
    (plain structured JSON per file, no nested JSON-in-a-string layer)."""
    visual = visual_doc.get("visual", {}) or {}
    position = visual_doc.get("position", {}) or {}
    fields = sorted(set(str(f) for f in _find_all(visual, "queryRef")))

    sync = visual.get("syncGroup")
    body = {k: v for k, v in visual_doc.items() if k != "filterConfig"}
    return {
        "visual_type": visual.get("visualType"),
        "title": _extract_title(visual.get("visualContainerObjects", {}) or {}),
        "fields": fields,
        "field_refs": sorted(_field_refs(body)),
        # PBIR hidden flag (isHidden) is unverified: no PBIR file tested had one.
        "hidden": bool(visual_doc.get("isHidden") or visual.get("isHidden")),
        "sync_group": sync.get("groupName") if isinstance(sync, dict) else None,
        "filters": _extract_filters(visual_doc.get("filterConfig")),
        "x": position.get("x"),
        "y": position.get("y"),
        "width": position.get("width"),
        "height": position.get("height"),
    }


# Legacy section.config "type" values seen across 33 real files: 0 = default
# page, 2 = drillthrough (the same pages are "Drillthrough" in PBIR). Any other
# value is reported verbatim rather than guessed.
_LEGACY_PAGE_TYPES = {0: "default", 2: "drillthrough"}


def _page_kind_pbir(page_doc: dict) -> str:
    raw = page_doc.get("type") or (page_doc.get("pageBinding") or {}).get("type") or "Default"
    low = str(raw).lower()
    if "drill" in low:
        return "drillthrough"
    if "tooltip" in low:
        return "tooltip"
    return "default" if low == "default" else f"unknown({raw})"


def _mark_off_canvas(visuals: list[dict[str, Any]], page_w: Any, page_h: Any) -> None:
    for v in visuals:
        x, y, w, h = v.get("x"), v.get("y"), v.get("width"), v.get("height")
        if "error" in v or None in (x, y, w, h) or not page_w or not page_h:
            v["off_canvas"] = False
            continue
        v["off_canvas"] = x >= page_w or y >= page_h or (x + w) <= 0 or (y + h) <= 0


def _summarize_report(pages: list[dict[str, Any]], bookmarks: list[str]) -> dict[str, Any]:
    """Things a viewer can't see from the rendered report but that change what
    it shows: hidden pages, drillthrough-only pages, off-canvas/hidden
    visuals, bookmarks (saved filter/visibility states), and slicer sync
    groups (a slicer on one page silently filtering others)."""
    sync: dict[str, set[str]] = {}
    off, hidden_v = [], 0
    for p in pages:
        for v in p["visuals"]:
            if v.get("off_canvas"):
                off.append({"page": p["display_name"], "visual_type": v.get("visual_type"), "title": v.get("title")})
            hidden_v += int(bool(v.get("hidden")))
            if v.get("sync_group"):
                sync.setdefault(v["sync_group"], set()).add(p["display_name"])
    return {
        "hidden_pages": [p["display_name"] for p in pages if p.get("hidden")],
        "drillthrough_pages": [p["display_name"] for p in pages if p.get("page_type") == "drillthrough"],
        "tooltip_pages": [p["display_name"] for p in pages if p.get("page_type") == "tooltip"],
        "hidden_visual_count": hidden_v,
        "off_canvas_visuals": off,
        "bookmarks": bookmarks,
        "bookmark_count": len(bookmarks),
        "slicer_sync_groups": {k: sorted(v) for k, v in sync.items() if len(v) > 1},
        "notes": "Verified against 41 real files: hidden pages, drillthrough pages, slicer sync groups. UNVERIFIED (no positive example in any of those files): bookmarks, hidden visuals, tooltip pages, off-canvas visuals -- detection follows the file formats and ran without error, but has never been seen firing on real data.",
    }


def _parse_layout_legacy(zf: zipfile.ZipFile, pbix_path: Path) -> dict[str, Any]:
    raw = zf.read("Report/Layout")
    layout_doc = json.loads(_decode_layout_bytes(raw))

    pages = []
    for section in layout_doc.get("sections", []):
        visuals = []
        for container in section.get("visualContainers", []):
            try:
                visuals.append(_parse_visual_legacy(container))
            except Exception as exc:  # noqa: BLE001
                visuals.append({"error": f"Could not parse visual: {exc}"})

        scfg = _maybe_json(section.get("config")) or {}
        scfg = scfg if isinstance(scfg, dict) else {}
        ptype = scfg.get("type")
        _mark_off_canvas(visuals, section.get("width"), section.get("height"))
        pages.append(
            {
                "name": section.get("name"),
                "display_name": section.get("displayName"),
                "ordinal": section.get("ordinal"),
                "hidden": scfg.get("visibility") == 1,
                "page_type": "default" if ptype is None else _LEGACY_PAGE_TYPES.get(ptype, f"unknown(type={ptype})"),
                "width": section.get("width"),
                "height": section.get("height"),
                "visual_count": len(visuals),
                "filters": _extract_filters(section.get("filters")),
                "visuals": visuals,
            }
        )

    lcfg = _maybe_json(layout_doc.get("config")) or {}
    raw_bm = (lcfg.get("bookmarks") or []) if isinstance(lcfg, dict) else []
    bookmarks = [str(b.get("displayName") or b.get("name")) for b in raw_bm if isinstance(b, dict)]
    return {
        "file": str(pbix_path),
        "format": "legacy",
        "report_filters": _extract_filters(layout_doc.get("filters")),
        "page_count": len(pages),
        "summary": _summarize_report(pages, bookmarks),
        "pages": pages,
    }


def _parse_layout_pbir(zf: zipfile.ZipFile, pbix_path: Path) -> dict[str, Any]:
    pages_meta = json.loads(zf.read("Report/definition/pages/pages.json").decode("utf-8"))
    page_order = pages_meta.get("pageOrder", [])
    all_names = set(zf.namelist())

    report_filters: list[dict[str, Any]] = []
    if "Report/definition/report.json" in all_names:
        try:
            report_doc = json.loads(zf.read("Report/definition/report.json").decode("utf-8"))
            report_filters = _extract_filters(report_doc.get("filterConfig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    pages = []
    for ordinal, page_id in enumerate(page_order):
        page_json_path = f"Report/definition/pages/{page_id}/page.json"
        display_name = page_id
        page_filters: list[dict[str, Any]] = []
        page_doc: dict[str, Any] = {}
        if page_json_path in all_names:
            try:
                page_doc = json.loads(zf.read(page_json_path).decode("utf-8"))
                display_name = page_doc.get("displayName", page_id)
                page_filters = _extract_filters(page_doc.get("filterConfig"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                page_doc = {}

        visual_prefix = f"Report/definition/pages/{page_id}/visuals/"
        visual_json_paths = sorted(
            n for n in all_names if n.startswith(visual_prefix) and n.endswith("/visual.json")
        )

        visuals = []
        for vpath in visual_json_paths:
            try:
                visual_doc = json.loads(zf.read(vpath).decode("utf-8"))
                visuals.append(_parse_visual_pbir(visual_doc))
            except Exception as exc:  # noqa: BLE001
                visuals.append({"error": f"Could not parse {vpath}: {exc}"})

        _mark_off_canvas(visuals, page_doc.get("width"), page_doc.get("height"))
        pages.append(
            {
                "name": page_id,
                "display_name": display_name,
                "ordinal": ordinal,
                "hidden": page_doc.get("visibility") == "HiddenInViewMode",
                "page_type": _page_kind_pbir(page_doc),
                "width": page_doc.get("width"),
                "height": page_doc.get("height"),
                "visual_count": len(visuals),
                "filters": page_filters,
                "visuals": visuals,
            }
        )

    bookmarks: list[str] = []
    for n in sorted(all_names):
        if n.startswith("Report/definition/bookmarks/") and n.endswith(".bookmark.json"):
            try:
                bm = json.loads(zf.read(n).decode("utf-8"))
                bookmarks.append(str(bm.get("displayName") or bm.get("name") or n))
            except (json.JSONDecodeError, UnicodeDecodeError):
                bookmarks.append(n)
    return {
        "file": str(pbix_path),
        "format": "pbir",
        "report_filters": report_filters,
        "page_count": len(pages),
        "summary": _summarize_report(pages, bookmarks),
        "pages": pages,
    }


def parse_layout(pbix_path: str | Path) -> dict[str, Any]:
    """Parse a .pbix file's report layout offline. Handles both report
    formats Power BI Desktop can save: the legacy single Report/Layout blob,
    and the newer PBIR format (one JSON file per page/visual under
    Report/definition/), which has been the default for new files since
    late-2024 Power BI Desktop builds.
    """
    pbix_path = Path(pbix_path)
    if not pbix_path.exists():
        raise FileNotFoundError(f"No such file: {pbix_path}")

    with zipfile.ZipFile(pbix_path) as zf:
        names = set(zf.namelist())
        if "Report/Layout" in names:
            return _parse_layout_legacy(zf, pbix_path)
        if "Report/definition/pages/pages.json" in names:
            return _parse_layout_pbir(zf, pbix_path)

    raise ValueError(
        f"{pbix_path.name} has no recognizable report layout (neither "
        "Report/Layout nor Report/definition/pages/pages.json) -- it may be "
        "a template (.pbit) with no saved report, or a corrupt file."
    )


# Folders under the user's profile that commonly hold Power BI files.
_HOME_SUBDIRS = ["Desktop", "Documents", "Downloads", os.path.join("OneDrive", "Desktop")]
# OneDrive / SharePoint-synced folders are found through %OneDriveCommercial% and %OneDrive%, so this
# works whatever the organization or profile is called. Add any other folder (for example a synced
# SharePoint team library) with POWERBI_MCP_SCAN_DIRS, a semicolon-separated list.
_ONEDRIVE_SUBDIRS = ["Desktop", "Documents"]


def default_search_dirs() -> list[str]:
    home = Path.home()
    dirs = [str(home / d) for d in _HOME_SUBDIRS]
    roots: list[Path] = []
    for var in ("OneDriveCommercial", "OneDrive"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    # Extra folders (semicolon-separated) for other machines/layouts.
    dirs.extend(d.strip() for d in os.environ.get("POWERBI_MCP_SCAN_DIRS", "").split(";") if d.strip())
    seen: set[str] = set()
    for r in roots:
        if str(r).lower() in seen or not r.exists():
            continue
        seen.add(str(r).lower())
        dirs.extend(str(r / sub) for sub in _ONEDRIVE_SUBDIRS)
    return dirs


def scan_pbix_files(search_dirs: list[str] | None = None, max_results: int = 300) -> dict[str, Any]:
    """Scan folders (recursively) for .pbix / .pbit files.

    Returns {"files", "count", "truncated", "searched", "missing_dirs"}. Never
    stops silently: `truncated` is true when more files exist than `max_results`.
    Files are newest-modified first.
    """
    dirs = search_dirs or default_search_dirs()
    found: dict[str, dict[str, Any]] = {}
    searched, missing = [], []
    for d in dirs:
        base = Path(d)
        if not base.exists():
            missing.append(str(base))
            continue
        searched.append(str(base))
        try:
            candidates = list(base.rglob("*.pb*"))
        except OSError:
            continue
        for path in candidates:
            if path.suffix.lower() not in (".pbix", ".pbit"):
                continue
            try:
                resolved = str(path.resolve())
                if resolved.lower() in found:
                    continue
                stat = path.stat()
            except OSError:
                continue
            found[resolved.lower()] = {"path": resolved, "size_bytes": stat.st_size, "modified": stat.st_mtime}
    files = sorted(found.values(), key=lambda f: f["modified"], reverse=True)
    return {
        "files": files[:max_results],
        "count": min(len(files), max_results),
        "total_found": len(files),
        "truncated": len(files) > max_results,
        "searched": searched,
        "missing_dirs": missing,
    }


def find_pbix_files(search_dirs: list[str] | None = None, max_results: int = 300) -> list[dict[str, Any]]:
    """List form of scan_pbix_files (kept for internal callers)."""
    return scan_pbix_files(search_dirs, max_results)["files"]
