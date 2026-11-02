"""MCP tool surface for the offline project engine and the guarded live-write layer.

Plain functions, registered on the shared FastMCP-style server by `register(mcp)`. Read tools never
write. Write tools are two-step (plan_* returns a diff and writes nothing; apply_change writes) or
dry-run-by-default (the *_live tools).
"""
from __future__ import annotations

import inspect
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

from . import analysis, bpa, changes, checks, daxrefs, edits, live, pbir, project as prj, scaffold
from .model import DictModel, Model

PLUS_VERSION = "4.0.0"
_DEFAULT_DIRS = ("~/Desktop", "~/Documents", "~/Downloads", "~/OneDrive*")


# ------------------------------------------------------------------------------ helpers
def _project(path: str) -> prj.Project:
    return prj.locate(path)


def _need_tmdl(p: prj.Project) -> Model:
    if p.tmdl_dir is None:
        raise ValueError("This project's semantic model is not saved as TMDL. In Power BI Desktop: File > Save as > Power BI Project "
                         "(enable 'Store semantic model using TMDL format' under Options > Preview features), then pass the new .pbip.")
    return Model.load(p.tmdl_dir)


def _need_pbir(p: prj.Project) -> pbir.Report:
    if p.pbir_dir is None:
        raise ValueError("This project has no PBIR report definition (report folder missing, or it still uses the legacy report.json). "
                         "In Desktop, enable 'Store reports using enhanced metadata format (PBIR)' under Options > Preview features and re-save.")
    return pbir.Report.load(p.pbir_dir)


def _expand_dirs(dirs: list[str] | None) -> list[str]:
    if dirs:
        return dirs
    import glob
    out: list[str] = []
    for pat in _DEFAULT_DIRS:
        out += glob.glob(os.path.expanduser(pat))
    return out


# ------------------------------------------------------------------------------ read tools
def plugin_status() -> dict[str, Any]:
    """Report the plugin version, runtime, safety switches and which capability layers are usable right now
    (offline project engine, live Desktop connection, write guards). Call this first when something seems unavailable."""
    from ..discovery import find_adomd_dll, find_running_instances
    status: dict[str, Any] = {"plus_version": PLUS_VERSION, "python": sys.version.split()[0], "platform": platform.platform(),
                              "offline_project_engine": "ok", "writes_enabled": changes.writes_enabled(),
                              "edit_root": os.environ.get("PBI_MCP_EDIT_ROOT") or None, "backup_dir": str(changes.backup_root())}
    try:
        status["adomd_dll"] = str(find_adomd_dll())
        status["live_connection"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["live_connection"] = f"unavailable: {exc}"
    try:
        status["open_files"] = [i.display_name for i in find_running_instances()]
    except Exception as exc:  # noqa: BLE001
        status["open_files"] = f"could not list: {exc}"
    return status


def find_pbip_projects(search_dirs: list[str] | None = None, max_results: int = 200) -> dict[str, Any]:
    """Find Power BI Project (.pbip) folders on disk, newest first, with each project's model format (tmdl/bim) and report
    format (pbir/pbir-legacy). Defaults to Desktop, Documents, Downloads and OneDrive. Offline; Power BI Desktop is not needed."""
    return prj.find_projects(_expand_dirs(search_dirs), max_results)


def inspect_project(path: str) -> dict[str, Any]:
    """One-call overview of a .pbip project (path = the .pbip, the project folder, or its .Report/.SemanticModel folder):
    formats, model counts, report pages/visuals, and validation counts. Offline."""
    p = _project(path)
    out: dict[str, Any] = {"project": p.describe()}
    if p.tmdl_dir:
        m = Model.load(p.tmdl_dir)
        d = m.to_dict(include_expressions=False)
        out["model"] = {"counts": d["counts"], "tables": [{"name": t["name"], "hidden": t["hidden"], "calculated": t["calculated"],
                                                           "columns": len(t["columns"]), "measures": len(t["measures"])} for t in d["tables"]],
                        "roles": [r["name"] for r in d["roles"]], "shared_queries": [e["name"] for e in d["expressions"]]}
    if p.pbir_dir:
        s = pbir.Report.load(p.pbir_dir).summary()
        out["report"] = {"pages": [{"id": pg["id"], "name": pg["display_name"], "visuals": pg["visual_count"], "hidden": pg["hidden"]} for pg in s["pages"]],
                         "bookmarks": s["bookmark_count"], "theme": s["theme"]}
    v = checks.validate_project(p)
    out["validation"] = {"ok": v["ok"], "counts": v["counts"], "top": v["findings"][:10]}
    return out


def read_model_tmdl(path: str, table: str | None = None, include_expressions: bool = True) -> dict[str, Any]:
    """Read a project's semantic model from its TMDL files: tables, columns, measures (DAX), hierarchies, partitions (M),
    relationships, RLS roles, shared queries, functions. Pass `table` to limit to one table. Offline."""
    d = _need_tmdl(_project(path)).to_dict(include_expressions)
    if table:
        d["tables"] = [t for t in d["tables"] if t["name"].lower() == table.lower()]
        if not d["tables"]:
            raise ValueError(f"No table {table!r}.")
    return d


def get_report_definition(path: str, page: str | None = None) -> dict[str, Any]:
    """Read a project's PBIR report: pages, visuals (type, title, position, hidden, alt text, fields used), bookmarks, theme.
    Pass `page` (id or display name) to see one page. Offline."""
    s = _need_pbir(_project(path)).summary()
    if page:
        s["pages"] = [pg for pg in s["pages"] if page in (pg["id"], pg["display_name"])]
        if not s["pages"]:
            raise ValueError(f"No page {page!r}.")
    return s


def validate_project(path: str) -> dict[str, Any]:
    """Check a project is internally consistent: TMDL parses, no duplicate/clashing names, relationships and sort-by columns
    exist, DAX is balanced, measure graph has no cycles, every report visual/filter field exists in the model, pages and
    visuals are well formed. Findings are ranked high/medium/info. This is the same gate every edit must pass. Offline."""
    return checks.validate_project(_project(path))


def best_practice_scan(target: str, rules: list[str] | None = None) -> dict[str, Any]:
    """Best-practice analysis of a semantic model: bidirectional/many-to-many relationships, visible foreign keys, summarizable
    IDs, missing date table, auto date/time bloat, DAX smells (IFERROR, FILTER(ALL), nested CALCULATE, '/' vs DIVIDE...),
    missing formats, month-name sort, empty RLS roles. `target` is a .pbip path/project folder (offline) OR the name of an
    open Power BI Desktop file (live). Static analysis: findings are leads, not measurements."""
    if Path(target).expanduser().exists():
        model: Model = _need_tmdl(_project(target))
    else:
        model = DictModel(live.snapshot_model(target))
    return bpa.run(model, set(rules) if rules else None)


def lint_dax(expression: str) -> dict[str, Any]:
    """Static check of one DAX expression: balanced brackets/quotes, the references it makes, and known performance or
    correctness smells. Does not execute anything (use validate_dax_live to have the engine compile it)."""
    return {"problems": daxrefs.balanced(expression), "references": [f"{r.table}[{r.name}]" if r.table else f"[{r.name}]" for r in daxrefs.refs(expression)],
            "lint": [{"rule": l.rule, "severity": l.severity, "message": l.message} for l in daxrefs.lint(expression)]}


def measure_dependencies(path: str) -> dict[str, Any]:
    """Measure-to-measure and measure-to-column dependency graph from a project's TMDL: who uses whom, measures nothing
    references, unresolved references, and circular chains. Offline."""
    return _need_tmdl(_project(path)).measure_graph()


def find_unused_fields_project(path: str) -> dict[str, Any]:
    """Columns and measures that no report visual, filter, relationship, sort-by, hierarchy or DAX expression uses (offline,
    from the saved project). Leads, not a delete list; read the `caveat`."""
    return analysis.unused_fields(_project(path))


def classify_sources_project(path: str) -> dict[str, Any]:
    """Which data source (SQL Server, SharePoint list/files, Excel, CSV, Web, OData, inline table...) each table and shared
    query loads from, by static reading of the M text in the project's TMDL. Never shows credentials."""
    return {"sources": analysis.classify_sources(_need_tmdl(_project(path)))}


def analyze_project_queries(path: str, aspect: str = "all", include_steps: bool = False) -> dict[str, Any]:
    """Power Query (M) diagnosis of a SAVED project without opening Desktop: aspect = connections | sql | folding | sharepoint |
    dependencies | all. Classifies every source (SQL Server, SharePoint, OneDrive, files, web), lints embedded SQL, estimates where
    query folding ends, finds SharePoint anti-patterns (including swallowed errors) and maps refresh dependencies. Static reading
    of M text: leads, not proof."""
    return analysis.analyze_queries(_need_tmdl(_project(path)), aspect, include_steps)


def document_project(path: str, output_path: str | None = None, include_dax: bool = True) -> dict[str, Any]:
    """Generate full Markdown documentation for a project: sources, tables/columns, every measure with its DAX, dependencies,
    relationships, RLS, report pages and visuals, validation summary. Returns the text; if `output_path` (a .md file outside
    the project's .Report/.SemanticModel folders) is given, also saves it there."""
    p = _project(path)
    md = analysis.document(p, include_dax)
    out: dict[str, Any] = {"markdown": md, "chars": len(md)}
    if output_path:
        target = Path(output_path).expanduser().resolve()
        if target.suffix.lower() != ".md":
            raise ValueError("output_path must end in .md")
        changes.check_write_target(target)
        if not changes._inside(target, p.root):
            raise ValueError(f"Save documentation inside the project folder ({p.root}), outside .Report and .SemanticModel.")
        for protected in (p.model_dir, p.report_dir):
            if protected is not None and changes._inside(target, protected):
                raise ValueError("Write documentation outside the .Report and .SemanticModel folders.")
        if any(part.startswith(".") for part in target.relative_to(p.root.resolve()).parts) or target.name.lower() in (
                "claude.md", "agents.md", "gemini.md", "copilot-instructions.md"):
            raise ValueError("Documentation cannot go in a dot-folder or use an agent-instruction file name.")
        if target.exists():
            raise ValueError(f"{target} already exists; choose a new file name (nothing was overwritten).")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md, encoding="utf-8")
        out["saved_to"] = str(target)
    return out


def diff_projects(left: str, right: str) -> dict[str, Any]:
    """Compare two projects (model: tables, columns, measures, relationships, roles; report: pages and visuals).
    Use for before/after an edit or backup-vs-current. Offline."""
    return analysis.diff_projects(_project(left), _project(right))


# ------------------------------------------------------------------------------ new / copy
def create_project(parent_folder: str, name: str, with_sample: bool = True) -> dict[str, Any]:
    """Create a brand-new Power BI Project (TMDL model + PBIR report) in `parent_folder/name`. With `with_sample` it holds an
    inline-data Sales/Region model, three measures and two visuals that open with no credentials. Refuses to overwrite.
    Open the .pbip in Power BI Desktop (preview features for PBIR/TMDL may need enabling)."""
    return scaffold.create_project(parent_folder, name, with_sample)


def copy_project(source: str, destination_folder: str, new_name: str | None = None) -> dict[str, Any]:
    """Copy a project (without cache.abf) to a new folder so edits can be made on a copy. Refuses to overwrite and
    re-points the .pbip/.pbir to the copy's own folders. For a .pbix: open it in Desktop and File > Save as .pbip first."""
    return prj.copy_project(source, destination_folder, new_name)


# ------------------------------------------------------------------------------ plan / apply
def _call(obj: Any, op: str, params: dict[str, Any], allowed: set[str]) -> Any:
    if op not in allowed:
        raise ValueError(f"Unknown operation {op!r}. Choose one of: {', '.join(sorted(allowed))}.")
    fn = getattr(obj, op)
    try:
        inspect.signature(fn).bind(**(params or {}))
    except TypeError as exc:
        raise ValueError(f"Bad parameters for {op}: {exc}. Signature: {op}{inspect.signature(fn)}") from exc
    return fn(**(params or {}))


MODEL_OPS = {"upsert_measure", "upsert_calculated_column", "set_properties", "delete_object", "rename", "move_measure", "add_relationship",
             "delete_relationship", "set_relationship", "upsert_role", "set_partition_source", "upsert_shared_expression"}


def plan_model_change(path: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Plan ONE edit to a project's TMDL semantic model. Writes nothing; returns a diff, the files touched and any NEW errors the
    edit would cause (found by trial-applying to a temp copy). operation + params:
      upsert_measure {table,name,expression,format_string?,description?,display_folder?,hidden?}
      upsert_calculated_column {table,name,expression,data_type?,format_string?,description?,hidden?}
      set_properties {kind: measure|column|table, table, name, props: {formatString,displayFolder,description,isHidden,dataCategory,summarizeBy,sortByColumn}}
      delete_object {kind: table|measure|column|hierarchy, table, name?}
      rename {kind: table|measure|column, table, old?, new}  (rewrites DAX, relationships, sort-by, RLS and report visuals)
      move_measure {from_table, name, to_table}
      add_relationship {from_table,from_column,to_table,to_column,cross_filter?,active?,from_cardinality?,to_cardinality?}
      delete_relationship {from_table,from_column,to_table,to_column}   set_relationship {..., active?, cross_filter?}
      upsert_role {name, filters: {table: DAX filter}}   set_partition_source {table, m_expression}
      upsert_shared_expression {name, m_expression, description?}
    Show the user the diff; only after approval call apply_change(plan_id)."""
    p = _project(path)
    ed = edits.Editor(p)
    if operation == "set_properties" and "props" in (params or {}):
        params = {**params}
        props = params.pop("props")
        desc = ed.set_properties(params["kind"], params["table"], params["name"] if "name" in params else "", props)
    else:
        desc = _call(ed, operation, params, MODEL_OPS)
    return changes.plan(p, ed.edits(), desc)


REPORT_OPS = {"add_page", "delete_page", "set_page", "reorder_pages", "add_visual", "update_visual", "delete_visual", "apply_theme", "create_theme"}


def plan_report_change(path: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Plan ONE edit to a project's PBIR report. Writes nothing; returns a diff and any NEW errors. operation + params:
      add_page {display_name,width?,height?,make_active?}      delete_page {page}
      set_page {page,display_name?,hidden?,width?,height?}      reorder_pages {order:[page,...]}
      add_visual {page, visual_type, fields:[{kind:Column|Measure,table,name,role?}], x,y,width,height, title?}
         visual_type: card, slicer, tableEx, pivotTable, clusteredBarChart, clusteredColumnChart, stackedBarChart, stackedColumnChart,
         lineChart, areaChart, pieChart, donutChart, scatterChart, gauge, kpi, waterfallChart, funnel, treemap, map, textbox, shape, image, actionButton
      update_visual {page,visual_id,title?,alt_text?,x?,y?,width?,height?,hidden?}      delete_visual {page,visual_id}
      apply_theme {name, theme:{...theme json...}}      create_theme {name, colors:[#hex,...], background?, foreground?}  (builds and applies a theme)
    Show the user the diff; only after approval call apply_change(plan_id)."""
    p = _project(path)
    rep = _need_pbir(p)
    params = dict(params or {})
    if operation not in REPORT_OPS:
        raise ValueError(f"Unknown operation {operation!r}. Choose one of: {', '.join(sorted(REPORT_OPS))}.")
    fn = getattr(pbir, operation, None)
    if fn is not None and operation not in ("apply_theme", "create_theme"):
        sig = inspect.signature(fn)
        if "page" in params and "page_ref" in sig.parameters:
            params["page_ref"] = params.pop("page")
        try:
            sig.bind(rep, **params)
        except TypeError as exc:
            raise ValueError(f"Bad parameters for {operation}: {exc}. Signature: {operation}{sig}") from exc
    if operation == "add_page":
        pid, e = pbir.add_page(rep, **params)
        desc = f"add page {params.get('display_name')!r} ({pid})"
    elif operation == "add_visual":
        vid, e = pbir.add_visual(rep, **params)
        desc = f"add {params.get('visual_type')} visual {vid} to page {params.get('page_ref')!r}"
    elif operation == "create_theme":
        theme = theme_from_colors(params["name"], params["colors"], params.get("background"), params.get("foreground"))
        e = pbir.apply_theme_edit(rep, params["name"], theme)
        desc = f"create and apply theme {params['name']!r}"
    elif operation == "apply_theme":
        e = pbir.apply_theme_edit(rep, params["name"], params["theme"])
        desc = f"apply theme {params['name']!r}"
    else:
        e = fn(rep, **params)
        desc = f"{operation} {params}"
    base = p.pbir_dir
    return changes.plan(p, {(base / rel).resolve(): text for rel, text in e.items()}, desc)


def theme_from_colors(name: str, colors: list[str], background: str | None = None, foreground: str | None = None) -> dict[str, Any]:
    if not colors or not all(re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in colors):
        raise ValueError("colors must be a non-empty list of #RRGGBB values.")
    fg = foreground or "#252423"
    return {"name": name, "dataColors": colors, "background": background or "#FFFFFF", "foreground": fg, "tableAccent": colors[0],
            "textClasses": {"title": {"fontFace": "Segoe UI Semibold", "fontSize": 14, "color": fg},
                            "label": {"fontFace": "Segoe UI", "fontSize": 10, "color": fg}}}


def apply_change(plan_id: str) -> dict[str, Any]:
    """Apply a plan made by plan_model_change/plan_report_change, ONLY after the user approved its diff. Backs up the files
    outside the project, writes atomically, re-validates the whole project, and restores the backup automatically if the edit
    introduced a new high-severity error. Power BI Desktop does not hot-reload: the user must reopen the project."""
    return changes.apply(plan_id)


def list_project_backups(path: str | None = None, limit: int = 30) -> dict[str, Any]:
    """List the automatic backups made by apply_change (newest first), optionally for one project."""
    return changes.list_backups(path, limit)


def restore_backup(backup_id: str, confirm: bool = False) -> dict[str, Any]:
    """Undo an applied change by restoring its backup. Without confirm=True it only previews which files would be restored; call
    again with confirm=True after the user agrees. Refuses if the files were modified after that change, so later work is never erased."""
    return changes.restore(backup_id, confirm)


# ------------------------------------------------------------------------------ live (open Desktop file)
def validate_dax_live(instance: str, table: str, expression: str) -> dict[str, Any]:
    """Have the open model's engine compile and evaluate a candidate measure expression in `table` WITHOUT saving it. Returns
    whether it compiles, a sample value or the engine's error text, plus static lint."""
    return live.validate_dax(instance, table, expression)


def upsert_measure_live(instance: str, table: str, name: str, expression: str | None = None, format_string: str | None = None,
                        description: str | None = None, display_folder: str | None = None, hidden: bool | None = None,
                        dry_run: bool = True, confirm_token: str | None = None) -> dict[str, Any]:
    """Create or update a measure in the model of an OPEN Power BI Desktop file (via TMSL). dry_run=True (default) compiles the
    candidate in the engine, shows before/after and returns a confirm_token; nothing changes. Call with dry_run=False and that
    confirm_token only after the user approved the shown change (a write without a prior preview is refused). The change exists only in Desktop's open session until the user saves the file; a backup allows undo_live_change.
    Omit `expression` to change only format/description/folder/hidden of an existing measure."""
    return live.upsert_measure(instance, table, name, expression, format_string=format_string, description=description,
                               display_folder=display_folder, hidden=hidden, dry_run=dry_run, confirm_token=confirm_token)


def delete_measure_live(instance: str, table: str, name: str, dry_run: bool = True, confirm_token: str | None = None) -> dict[str, Any]:
    """Delete a measure from an OPEN Desktop file's model. dry_run=True (default) lists measures that reference it, returns a
    confirm_token and changes nothing; the real delete needs that token and refuses while other measures still reference it."""
    return live.delete_measure(instance, table, name, dry_run, confirm_token)


def undo_live_change(instance: str, backup_id: str, confirm: bool = False) -> dict[str, Any]:
    """Revert a change made by upsert_measure_live/delete_measure_live using its backup_id (before the file is saved).
    Without confirm=True it only previews; call again with confirm=True after the user agrees."""
    return live.undo(instance, backup_id, confirm)


ALL_TOOLS = [
    plugin_status, find_pbip_projects, inspect_project, read_model_tmdl, get_report_definition, validate_project, best_practice_scan,
    lint_dax, measure_dependencies, find_unused_fields_project, classify_sources_project, analyze_project_queries, document_project, diff_projects,
    create_project, copy_project, plan_model_change, plan_report_change, apply_change, list_project_backups, restore_backup,
    validate_dax_live, upsert_measure_live, delete_measure_live, undo_live_change,
]

_WRITE_TOOLS = {"create_project", "copy_project", "apply_change", "restore_backup", "upsert_measure_live", "delete_measure_live", "undo_live_change", "document_project"}


def register(mcp) -> None:
    """Register every tool. Anything that can change a file or a live model is annotated as non-read-only."""
    try:
        from mcp.types import ToolAnnotations
    except Exception:  # noqa: BLE001
        ToolAnnotations = None  # type: ignore[assignment]
    for fn in ALL_TOOLS:
        if ToolAnnotations is not None:
            writes = fn.__name__ in _WRITE_TOOLS
            mcp.tool(annotations=ToolAnnotations(read_only_hint=not writes, destructive_hint=fn.__name__ in {"apply_change", "restore_backup", "delete_measure_live"},
                                                 open_world_hint=False))(fn)
        else:
            mcp.tool()(fn)
