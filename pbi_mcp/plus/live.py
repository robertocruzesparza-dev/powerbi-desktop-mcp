"""Write operations against the model of an OPEN Power BI Desktop file, through the Tabular Object Model (TOM) that ships inside Power BI Desktop.

Microsoft documents external-tool writes to Desktop's local Analysis Services instance as
supported (not processing/refresh, and not .pbit). Every write here is:
  1. dry-run first: the candidate is compiled and evaluated by the engine before anything changes;
  2. recorded: the previous definition is saved to a backup file so `undo` can replay it;
  3. read back after writing to confirm the engine holds exactly what was sent.
The change lands in Desktop's open session only: the user must still save the file in Desktop.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..discovery import find_adomd_dll
from ..engine import _load_adomd, resolve_instance, run_dax
from . import changes, daxrefs
from .tmdl import quote_name


class LiveError(ValueError):
    pass


_CONFIRMED: dict[str, float] = {}
_CONFIRM_TTL = 30 * 60


def _key(*parts: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _issue(key: str) -> str:
    now = time.time()
    for k in [k for k, t in _CONFIRMED.items() if t < now]:
        _CONFIRMED.pop(k, None)
    _CONFIRMED[key] = now + _CONFIRM_TTL
    return key


def _require(key: str, token: str | None) -> None:
    if token != key or _CONFIRMED.get(key, 0) < time.time():
        raise LiveError("This write was not previewed (or the model changed since). Call with dry_run=True first, show the user the "
                        "before/after, and pass the returned confirm_token with dry_run=False after they approve.")
    _CONFIRMED.pop(key, None)


def _dax_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _dax_measure(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _clean(name: str, what: str) -> str:
    if not name or any(ord(c) < 32 for c in name):
        raise LiveError(f"{what} must be non-empty text without control characters.")
    return name


_tom_lock = threading.Lock()
_tom_types: dict[str, Any] | None = None


def _load_tom() -> dict[str, Any]:
    """Load the Tabular Object Model that ships inside Power BI Desktop (Microsoft.PowerBI.Tabular.dll)."""
    global _tom_types
    with _tom_lock:
        if _tom_types is not None:
            return _tom_types
        _load_adomd()                                    # boots the CLR
        import clr
        from System.Reflection import Assembly
        bin_dir = find_adomd_dll().parent
        for name in ("Microsoft.PowerBI.Tabular.Json.dll", "Microsoft.PowerBI.Tabular.dll"):
            path = bin_dir / name
            if not path.is_file():
                raise LiveError(f"{name} not found in {bin_dir}; this Power BI Desktop build does not ship the Tabular Object Model.")
            Assembly.LoadFrom(str(path))
            clr.AddReference(str(path))
        from Microsoft.AnalysisServices.Tabular import Measure, Server  # type: ignore[import-not-found]
        _tom_types = {"Server": Server, "Measure": Measure}
        return _tom_types


@contextmanager
def _model(inst) -> Iterator[Any]:
    """Connected TOM Model of the (single) database in an open Desktop file."""
    types = _load_tom()
    server = types["Server"]()
    server.Connect(f"Data Source=localhost:{inst.port}")
    try:
        if server.Databases.Count < 1:
            raise LiveError("The open file has no model loaded.")
        yield server.Databases[0].Model
    finally:
        server.Disconnect()


def _catalog(port: int, inst) -> str:
    with _model(inst) as m:
        return str(m.Database.Name)


def _format_of(row: dict[str, Any]) -> str | None:
    """Static format string of a measure row, or None. INFO.VIEW.MEASURES leaves FormatString null when a format is set and
    shows it as a quoted FormatStringDefinition; a definition that is not a quoted literal is a dynamic format (DAX)."""
    fmt = row.get("FormatString")
    if fmt:
        return fmt
    d = row.get("FormatStringDefinition")
    if isinstance(d, str) and len(d) >= 2 and d[0] == d[-1] == '"' and '"' not in d[1:-1].replace('""', ""):
        return d[1:-1].replace('""', '"')
    return None


def _dynamic_format(row: dict[str, Any]) -> str | None:
    d = row.get("FormatStringDefinition")
    if isinstance(d, str) and d and _format_of(row) is None:
        return d
    return None


def _existing_measure(inst, name: str) -> dict[str, Any] | None:
    esc = name.replace('"', '""')
    rows = run_dax(inst, f'EVALUATE FILTER(INFO.VIEW.MEASURES(), [Name] = "{esc}")', 50)
    if not rows:
        return None
    if len(rows) > 1:
        raise LiveError(f"Measure name {name!r} matched {len(rows)} rows; cannot choose.")
    r = rows[0]
    return {"table": r.get("Table"), "name": r.get("Name"), "expression": r.get("Expression") or "", "format": _format_of(r),
            "hidden": bool(r.get("IsHidden")), "description": r.get("Description"), "display_folder": r.get("DisplayFolder"),
            "format_dynamic": _dynamic_format(r)}


def _table_exists(inst, table: str) -> bool:
    esc = table.replace('"', '""')
    return bool(run_dax(inst, f'EVALUATE FILTER(INFO.VIEW.TABLES(), [Name] = "{esc}")', 5))


def _compile_check(inst, table: str, name: str, expression: str) -> dict[str, Any]:
    """Ask the engine to compile and evaluate the candidate measure (nothing is stored)."""
    ref = f"{_dax_table(table)}{_dax_measure(name)}"
    q = f"DEFINE MEASURE {ref} = {expression}\nEVALUATE ROW(\"value\", {ref})"
    try:
        rows = run_dax(inst, q, 5)
        return {"compiles": True, "sample_value": rows[0].get("value") if rows else None}
    except Exception as exc:  # noqa: BLE001
        return {"compiles": False, "error": str(exc)[:800]}


def _live_backup_dir(db: str) -> Path:
    d = changes.backup_root() / "_live" / re.sub(r"[^\w\-]", "_", db)
    d.mkdir(parents=True, exist_ok=True)
    return d


def upsert_measure(instance: str, table: str, name: str, expression: str | None = None, *, format_string: str | None = None,
                   description: str | None = None, display_folder: str | None = None, hidden: bool | None = None,
                   dry_run: bool = True, confirm_token: str | None = None, _skip_confirm: bool = False) -> dict[str, Any]:
    if not changes.writes_enabled():
        raise LiveError("Writes are disabled: PBI_MCP_READONLY is set.")
    _clean(table, "table")
    _clean(name, "name")
    inst = resolve_instance(instance)
    if not _table_exists(inst, table):
        raise LiveError(f"Table {table!r} does not exist in the open model.")
    before = _existing_measure(inst, name)
    if before and before["table"] != table:
        raise LiveError(f"Measure {name!r} already exists in table {before['table']!r}; names are unique across the model.")
    if expression is None:
        if before is None:
            raise LiveError("expression is required to create a new measure.")
        expression = before["expression"]
    problems = daxrefs.balanced(expression)
    if problems:
        raise LiveError(f"Expression looks malformed: {', '.join(problems)}.")
    after = {"table": table, "name": name, "expression": expression.strip("\n"),
             "format": before["format"] if (before and format_string is None) else (format_string or None),
             "hidden": before["hidden"] if (before and hidden is None) else bool(hidden),
             "description": before["description"] if (before and description is None) else (description or None),
             "display_folder": before["display_folder"] if (before and display_folder is None) else (display_folder or None)}
    check = _compile_check(inst, table, name, after["expression"])
    view = {"action": "update" if before else "create", "measure": f"{table}[{name}]", "before": before, "after": after,
            "engine_check": check, "lint": [{"rule": l.rule, "severity": l.severity, "message": l.message} for l in daxrefs.lint(after["expression"])]}
    key = _key("upsert", inst.port, table, name, before, after)
    if dry_run:
        view["status"] = "dry_run"
        if check["compiles"]:
            view["confirm_token"] = _issue(key)
        view["next"] = ("Nothing was changed. " + ("The engine could not compile this expression; fix it first. " if not check["compiles"] else
                        "The engine compiled it. ") + "Show the user the before/after and call again with dry_run=False and confirm_token only after they approve.")
        return view
    if not _skip_confirm:
        _require(key, confirm_token)
    if not check["compiles"]:
        raise LiveError(f"The engine rejected the expression, so nothing was written: {check['error']}")
    db = _catalog(inst.port, inst)
    bid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    (_live_backup_dir(db) / f"{bid}.json").write_text(json.dumps({"database": db, "action": view["action"], "before": before, "after": after}, indent=1), encoding="utf-8")
    with _model(inst) as model:
        t = model.Tables.Find(table)
        if t is None:
            raise LiveError(f"Table {table!r} disappeared from the open model.")
        m = t.Measures.Find(name)
        if m is None:
            m = _load_tom()["Measure"]()
            m.Name = name
            t.Measures.Add(m)
        m.Expression = after["expression"]
        if not (before and before.get("format_dynamic") and format_string is None):   # never flatten a dynamic format string
            m.FormatString = after["format"] or None
        m.Description = after["description"] or None
        m.DisplayFolder = after["display_folder"] or None
        m.IsHidden = bool(after["hidden"])
        model.SaveChanges()
    readback = _existing_measure(inst, name)
    ok = bool(readback) and readback["table"] == table and readback["expression"].strip() == after["expression"].strip()
    view.update({"status": "applied" if ok else "applied_but_readback_differs", "backup_id": bid, "readback": readback,
                 "next": "The change is in Power BI Desktop's open session only. Tell the user to SAVE the file in Desktop to keep it; "
                         f"undo with undo_live_change('{bid}') before saving if needed."})
    return view


def delete_measure(instance: str, table: str, name: str, dry_run: bool = True, confirm_token: str | None = None,
                   _skip_confirm: bool = False) -> dict[str, Any]:
    if not changes.writes_enabled():
        raise LiveError("Writes are disabled: PBI_MCP_READONLY is set.")
    inst = resolve_instance(instance)
    before = _existing_measure(inst, name)
    if before is None or before["table"] != table:
        raise LiveError(f"Measure {table}[{name}] not found.")
    dependants = []
    for r in run_dax(inst, "EVALUATE SELECTCOLUMNS(INFO.VIEW.MEASURES(), \"n\", [Name], \"t\", [Table], \"e\", [Expression])", 5000):
        if r["n"] != name and any(x.name.lower() == name.lower() for x in daxrefs.refs(r["e"] or "")):
            dependants.append(f"{r['t']}[{r['n']}]")
    view = {"action": "delete", "measure": f"{table}[{name}]", "before": before, "measures_that_reference_it": dependants,
            "warning": "Visuals that use this measure will break; the file layout is not checked here (use validate_project on a saved PBIP)."}
    key = _key("delete", inst.port, table, name, before)
    if dry_run:
        view["status"] = "dry_run"
        view["confirm_token"] = _issue(key)
        view["next"] = "Nothing was changed. Show the user the impact and call again with dry_run=False and confirm_token only after they approve."
        return view
    if not _skip_confirm:
        _require(key, confirm_token)
    if dependants:
        raise LiveError(f"Refusing to delete: {', '.join(dependants)} reference it. Update or delete those first.")
    db = _catalog(inst.port, inst)
    bid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    (_live_backup_dir(db) / f"{bid}.json").write_text(json.dumps({"database": db, "action": "delete", "before": before, "after": None}, indent=1), encoding="utf-8")
    with _model(inst) as model:
        t = model.Tables.Find(table)
        m = t.Measures.Find(name) if t is not None else None
        if m is None:
            raise LiveError(f"Measure {table}[{name}] is no longer in the open model.")
        t.Measures.Remove(m)
        model.SaveChanges()
    gone = _existing_measure(inst, name) is None
    view.update({"status": "applied" if gone else "applied_but_still_present", "backup_id": bid,
                 "next": f"Removed in Desktop's open session only; SAVE to keep it, or undo_live_change('{bid}') to bring it back."})
    return view


def undo(instance: str, backup_id: str, confirm: bool = False) -> dict[str, Any]:
    import re as _re
    if not _re.fullmatch(r"[\w\-]+", backup_id or ""):
        raise LiveError("backup_id must be the id returned by a live write.")
    inst = resolve_instance(instance)
    db = _catalog(inst.port, inst)
    path = _live_backup_dir(db) / f"{backup_id}.json"
    if not path.is_file():
        raise LiveError(f"No live backup {backup_id!r} for this model.")
    rec = json.loads(path.read_text(encoding="utf-8"))
    before, after = rec["before"], rec["after"]
    name = (after or before)["name"]
    current = _existing_measure(inst, name)
    keys = ("table", "expression", "format", "hidden", "description", "display_folder")
    unchanged = current is not None and after is not None and all(current.get(k) == after.get(k) for k in keys)
    if after is None:
        unchanged = current is None
    if not unchanged:
        raise LiveError(f"Measure {name!r} was changed after that live edit, so undo would erase later work. Nothing was changed.")
    if not confirm:
        return {"status": "preview", "backup_id": backup_id, "would_restore": before, "would_remove": after if before is None else None,
                "next": "Nothing was changed. Ask the user, then call again with confirm=True."}
    if before is None:                       # it was a create: remove the measure again
        return delete_measure(instance, after["table"], after["name"], dry_run=False, _skip_confirm=True) | {"undone": backup_id}
    return upsert_measure(instance, before["table"], before["name"], before["expression"], format_string=before["format"] or "",
                          description=before["description"] or "", display_folder=before["display_folder"] or "",
                          hidden=before["hidden"], dry_run=False, _skip_confirm=True) | {"undone": backup_id}


def validate_dax(instance: str, table: str, expression: str, name: str = "__pbi_mcp_check__") -> dict[str, Any]:
    inst = resolve_instance(instance)
    if not _table_exists(inst, table):
        raise LiveError(f"Table {table!r} does not exist in the open model.")
    return {"balanced": daxrefs.balanced(expression) or "ok", **_compile_check(inst, table, name, expression),
            "lint": [{"rule": l.rule, "severity": l.severity, "message": l.message} for l in daxrefs.lint(expression)]}


def snapshot_model(instance: str) -> dict[str, Any]:
    """Live model in the same shape as Model.to_dict(), so best-practice rules can run on an open file."""
    inst = resolve_instance(instance)
    tables = {r["Name"]: {"name": r["Name"], "file": None, "hidden": bool(r.get("IsHidden")), "calculated": False, "data_category": r.get("DataCategory"),
                          "description": r.get("Description"), "columns": [], "measures": [], "hierarchies": [], "partitions": []}
              for r in run_dax(inst, "EVALUATE INFO.VIEW.TABLES()", 5000)}
    for r in run_dax(inst, "EVALUATE INFO.VIEW.COLUMNS()", 50000):
        t = tables.get(r.get("Table"))
        if t is None or str(r.get("Type", "")).lower() == "rowNumber".lower():
            continue
        t["columns"].append({"name": r.get("Name"), "data_type": (str(r.get("DataType") or "")).lower().replace("integer", "int64") or None,
                             "hidden": bool(r.get("IsHidden")), "is_key": bool(r.get("IsKey")), "calculated": bool(r.get("Expression")),
                             "expression": r.get("Expression"), "format": _format_of(r), "summarize_by": r.get("SummarizeBy"),
                             "sort_by": r.get("SortByColumn"), "display_folder": r.get("DisplayFolder"), "source_column": r.get("SourceColumn"),
                             "data_category": r.get("DataCategory"), "description": r.get("Description"), "line": None})
    for r in run_dax(inst, "EVALUATE INFO.VIEW.MEASURES()", 50000):
        t = tables.get(r.get("Table"))
        if t is not None:
            t["measures"].append({"name": r.get("Name"), "expression": r.get("Expression") or "", "format": _format_of(r),
                                  "hidden": bool(r.get("IsHidden")), "display_folder": r.get("DisplayFolder"), "description": r.get("Description"),
                                  "line": None, "lineage_tag": None})
    rels = []
    for r in run_dax(inst, "EVALUATE INFO.VIEW.RELATIONSHIPS()", 5000):
        rels.append({"id": None, "file": None, "line": None, "from_table": r.get("FromTable"), "from_column": r.get("FromColumn"),
                     "to_table": r.get("ToTable"), "to_column": r.get("ToColumn"), "active": bool(r.get("IsActive", True)),
                     "cross_filter": "bothDirections" if "both" in str(r.get("CrossFilteringBehavior", "")).lower() else "oneDirection",
                     "from_cardinality": "many" if "many" in str(r.get("FromCardinality", "many")).lower() else "one",
                     "to_cardinality": "many" if "many" in str(r.get("ToCardinality", "one")).lower() else "one", "join_on_date_behavior": None})
    return {"model": {"name": instance}, "tables": list(tables.values()), "relationships": rels, "roles": [], "expressions": [], "functions": [],
            "parse_errors": {}, "counts": {"tables": len(tables), "relationships": len(rels)}}
