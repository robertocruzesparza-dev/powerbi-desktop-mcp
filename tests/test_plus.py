"""Tests for the offline PBIP/TMDL/PBIR engine (pbi_mcp.plus). Run: python -m pytest tests -q"""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pbi_mcp.plus import analysis, bpa, changes, checks, daxrefs, edits, pbir, project as prj, scaffold, tmdl  # noqa: E402
from pbi_mcp.plus.model import Model  # noqa: E402


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("PBI_MCP_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.delenv("PBI_MCP_EDIT_ROOT", raising=False)
    monkeypatch.delenv("PBI_MCP_READONLY", raising=False)
    info = scaffold.create_project(str(tmp_path), "Demo")
    return prj.locate(info["pbip"])


def run_edit(project, fn):
    ed = edits.Editor(project)
    msg = fn(ed)
    p = changes.plan(project, ed.edits(), msg)
    return msg, p


# ------------------------------------------------------------------ tmdl
SRC = (
    "table Sales\n\tlineageTag: abc\n\n\t/// Total revenue\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n\t\tformatString: #,0\n"
    "\n\tmeasure Margin =\n\t\t\tVAR r = [Total Sales]\n\t\t\tRETURN\n\t\t\t    DIVIDE(r - [Cost], r)\n\t\tformatString: 0.0%\n"
    "\n\tcolumn Amount\n\t\tdataType: double\n\t\tisHidden\n\t\tsourceColumn: Amount\n"
)


def test_roundtrip_is_lossless():
    assert tmdl.parse(SRC).to_text() == SRC
    crlf = SRC.replace("\n", "\r\n")
    assert tmdl.parse(crlf).to_text() == crlf


def test_parse_objects_and_expressions():
    t = tmdl.parse(SRC).nodes[0]
    m = t.child("measure", "Total Sales")
    assert m.expr == "SUM(Sales[Amount])" and m.prop("formatString") == "#,0" and m.doc == ["Total revenue"]
    assert t.child("measure", "Margin").expr == "VAR r = [Total Sales]\nRETURN\n    DIVIDE(r - [Cost], r)"
    assert t.child("column", "Amount").flag("isHidden")


def test_fenced_expression():
    doc = tmdl.parse("table T\n\tmeasure M = ```\n\t\t\tSUM(T[x])\n\t\t\t```\n\t\tformatString: 0\n")
    m = doc.nodes[0].child("measure", "M")
    assert m.expr == "SUM(T[x])" and m.fenced and m.prop("formatString") == "0"


def test_quoting_helpers():
    assert tmdl.quote_name("Sales") == "Sales"
    assert tmdl.quote_name("Total Sales") == "'Total Sales'"
    assert tmdl.quote_name("O'Brien") == "'O''Brien'"
    assert tmdl.unquote_name("'O''Brien'") == "O'Brien"
    assert tmdl.split_dotted("'My Table'.'Col A'") == ["My Table", "Col A"]
    assert tmdl.split_dotted("Sales.Region") == ["Sales", "Region"]


def test_check_ignores_code_indent_but_flags_spaces():
    ok = tmdl.parse(SRC)
    assert tmdl.check(ok) == []
    bad = tmdl.parse("table T\n    column A\n")
    assert any("spaces" in p for p in tmdl.check(bad))


def test_unterminated_fence_is_an_error():
    with pytest.raises(tmdl.TmdlError):
        tmdl.parse("table T\n\tmeasure M = ```\n\t\t\tSUM(1)\n")


# ------------------------------------------------------------------ dax refs
def test_refs_ignore_strings_and_comments():
    r = daxrefs.refs("""SUM(Sales[Amount]) + [Total] // ignore [Fake]
        + "text [Nope]" + 'My Table'[Col A]""")
    assert [(x.table, x.name) for x in r] == [("Sales", "Amount"), (None, "Total"), ("My Table", "Col A")]


def test_rename_measure_and_column_in_expr():
    e, n = daxrefs.rename_in_expr("[Total] + Sales[Total] + Other[Total]", kind="measure", table="Sales", old="Total", new="Grand", owner_table="Sales")
    assert n == 2 and "Other[Total]" in e and "Sales[Grand]" in e and e.startswith("[Grand]")
    e, n = daxrefs.rename_in_expr("SUM(Sales[Amount]) + [Amount]", kind="column", table="Sales", old="Amount", new="Net", owner_table="Sales")
    assert n == 2 and "Sales[Net]" in e and "[Net]" in e
    e, n = daxrefs.rename_in_expr("SUM(Sales[Amount]) + [Amount]", kind="column", table="Sales", old="Amount", new="Net", owner_table="Region")
    assert n == 1


def test_rename_table_in_expr():
    e, n = daxrefs.rename_in_expr("SUM(Sales[Amount]) + COUNTROWS('Sales') + Sales2[x]", kind="table", table=None, old="Sales", new="Orders", owner_table=None)
    assert n == 2 and "Orders[Amount]" in e and "COUNTROWS(Orders)" in e and "Sales2[x]" in e


def test_lint_flags_known_smells():
    ids = {l.rule for l in daxrefs.lint("IFERROR(Sales[A] / Sales[B], 0)")}
    assert {"DAX001", "DAX011"} <= ids
    assert daxrefs.lint("DIVIDE(SUM(Sales[A]), SUM(Sales[B]))") == []
    assert daxrefs.balanced("SUM(x") == ["unbalanced parentheses"]


# ------------------------------------------------------------------ scaffold + model
def test_scaffold_is_valid(proj):
    assert proj.model_format == "tmdl" and proj.report_format == "pbir"
    res = checks.validate_project(proj)
    assert res["ok"], res["findings"]
    m = Model.load(proj.tmdl_dir)
    d = m.to_dict()
    assert d["counts"] == {"tables": 2, "measures": 3, "columns": 5, "relationships": 1}
    assert d["relationships"][0]["from_table"] == "Sales" and d["relationships"][0]["to_column"] == "Region"
    g = m.measure_graph()
    assert g["edges"]["Sales[Average Order]"]["measures"] == ["Sales[Order Count]", "Sales[Total Sales]"]
    assert g["cycles"] == []


def test_scaffold_refuses_existing(proj, tmp_path):
    with pytest.raises(prj.ProjectError):
        scaffold.create_project(str(tmp_path), "Demo")


def test_locate_variants(proj):
    for p in (proj.root, proj.pbip, proj.report_dir, proj.model_dir, proj.report_dir / "definition.pbir"):
        assert prj.locate(str(p)).name == "Demo"
    with pytest.raises(prj.ProjectError):
        prj.locate(str(proj.root / "nope"))


def test_find_projects_and_copy(proj, tmp_path):
    found = prj.find_projects([str(tmp_path)])
    assert found["count"] == 1 and found["projects"][0]["name"] == "Demo"
    (proj.model_dir / ".pbi").mkdir()
    (proj.model_dir / ".pbi" / "cache.abf").write_bytes(b"x")
    out = prj.copy_project(str(proj.pbip), str(tmp_path), "Demo2")
    assert out["name"] == "Demo2" and not list(Path(out["root"]).rglob("cache.abf"))
    assert json.loads((Path(out["root"]) / "Demo2.Report" / "definition.pbir").read_text())["datasetReference"]["byPath"]["path"] == "../Demo2.SemanticModel"
    assert checks.validate_project(prj.locate(out["pbip"]))["ok"]
    with pytest.raises(prj.ProjectError):
        prj.copy_project(str(proj.pbip), str(tmp_path), "Demo2")


# ------------------------------------------------------------------ validation catches problems
def test_validation_catches_model_and_report_breakage(proj):
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    f.write_text(f.read_text().replace("SUM(Sales[Amount])", "SUM(Sales[Amount]"), encoding="utf-8")
    res = checks.validate_project(proj)
    assert any(x["rule"] == "DAX100" for x in res["findings"]) and not res["ok"]


def test_validation_catches_visual_with_missing_field(proj):
    v = next(proj.pbir_dir.glob("pages/*/visuals/*/visual.json"))
    v.write_text(v.read_text().replace('"Total Sales"', '"Nope"'), encoding="utf-8")
    res = checks.validate_project(proj)
    assert any(x["rule"] == "PBIR051" for x in res["findings"])


# ------------------------------------------------------------------ edits: plan/apply/rollback
def test_upsert_measure_create_update_and_apply(proj):
    msg, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Big Orders", "CALCULATE([Order Count], Sales[Amount] > 100)", format_string="0", description="Orders over 100"))
    assert "created" in msg and p["predicted_new_errors"] == [] and "+\tmeasure 'Big Orders'" in p["diff"]
    before = (proj.tmdl_dir / "tables" / "Sales.tmdl").read_text()
    assert "Big Orders" not in before                                  # nothing written by plan
    res = changes.apply(p["plan_id"])
    assert res["status"] == "applied" and res["validation"]["ok"]
    m = Model.load(proj.tmdl_dir)
    hit = m.find_measure("Big Orders")[1].node
    assert hit.prop("formatString") == "0" and hit.description == "Orders over 100" and hit.prop("lineageTag")
    # update in place, other measures untouched byte-for-byte
    text_before = (proj.tmdl_dir / "tables" / "Sales.tmdl").read_text()
    _, p2 = run_edit(proj, lambda e: e.upsert_measure("Sales", "Big Orders", "CALCULATE([Order Count], Sales[Amount] > 150)"))
    changes.apply(p2["plan_id"])
    text_after = (proj.tmdl_dir / "tables" / "Sales.tmdl").read_text()
    assert "> 150" in text_after and "> 100" not in text_after
    assert text_after.replace("> 150", "> 100") == text_before


def test_malformed_dax_and_name_clashes_rejected(proj):
    with pytest.raises(edits.EditError):
        edits.Editor(proj).upsert_measure("Sales", "Bad", "SUM(Sales[Amount]")
    with pytest.raises(edits.EditError):
        edits.Editor(proj).upsert_measure("Region", "Total Sales", "1")     # same name, other table
    with pytest.raises(edits.EditError):
        edits.Editor(proj).upsert_measure("Sales", "Amount", "1")           # same name as a column


def test_stale_plan_is_refused(proj):
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "X", "1"))
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    f.write_text(f.read_text() + "\n", encoding="utf-8")
    with pytest.raises(changes.ChangeError, match="changed after"):
        changes.apply(p["plan_id"])


def test_delete_used_measure_is_flagged_and_rolled_back(proj):
    _, p = run_edit(proj, lambda e: e.delete_object("measure", "Sales", "Total Sales"))
    errs = {e["rule"] for e in p["predicted_new_errors"]}
    assert {"PBIR051", "DAX102"} <= errs  # visuals break, and measures that still reference it are now blocking too
    res = changes.apply(p["plan_id"])
    assert res["status"] == "rolled_back_validation_failed"
    assert Model.load(proj.tmdl_dir).find_measure("Total Sales")        # original restored


def test_restore_backup(proj):
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Temp", "1"))
    res = changes.apply(p["plan_id"])
    assert Model.load(proj.tmdl_dir).find_measure("Temp")
    assert changes.list_backups(str(proj.pbip))["total"] == 1
    out = changes.restore(res["backup_id"], confirm=True)
    assert out["status"] == "restored" and not Model.load(proj.tmdl_dir).find_measure("Temp")


def test_restore_refuses_when_file_changed_later(proj):
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Temp", "1"))
    res = changes.apply(p["plan_id"])
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    f.write_text(f.read_text() + "\n\n", encoding="utf-8")
    with pytest.raises(changes.ChangeError, match="later work"):
        changes.restore(res["backup_id"], confirm=True)


def test_edit_root_and_readonly_guards(proj, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PBI_MCP_EDIT_ROOT", str(other))
    with pytest.raises(changes.ChangeError, match="restricted"):
        run_edit(proj, lambda e: e.upsert_measure("Sales", "X", "1"))
    monkeypatch.delenv("PBI_MCP_EDIT_ROOT")
    monkeypatch.setenv("PBI_MCP_READONLY", "1")
    with pytest.raises(changes.ChangeError, match="disabled"):
        run_edit(proj, lambda e: e.upsert_measure("Sales", "X", "1"))


def test_rename_measure_updates_dax_and_report(proj):
    _, p = run_edit(proj, lambda e: e.rename("measure", "Sales", "Total Sales", "Revenue"))
    assert p["predicted_new_errors"] == []
    changes.apply(p["plan_id"])
    m = Model.load(proj.tmdl_dir)
    assert m.find_measure("Revenue") and not m.find_measure("Total Sales")
    assert "[Revenue]" in m.find_measure("Average Order")[1].node.expr
    rep = pbir.Report.load(proj.pbir_dir)
    names = {n for pg in rep.page_ids() for v in rep.visual_ids(pg) for _, _, n in pbir.unique_fields(rep.visual(pg, v))}
    assert "Revenue" in names and "Total Sales" not in names
    assert checks.validate_project(proj)["ok"]


def test_rename_column_updates_relationship_dax_and_report(proj):
    _, p = run_edit(proj, lambda e: e.rename("column", "Region", "Region", "Area"))
    changes.apply(p["plan_id"])
    d = Model.load(proj.tmdl_dir).to_dict()
    assert d["relationships"][0]["to_column"] == "Area"
    rep_text = "".join(f.read_text() for f in proj.pbir_dir.rglob("visual.json"))
    assert '"Area"' in rep_text
    assert checks.validate_project(proj)["ok"]


def test_rename_table_moves_file_and_rewrites_everything(proj):
    _, p = run_edit(proj, lambda e: e.rename("table", "Sales", None, "Orders"))
    assert p["predicted_new_errors"] == []
    changes.apply(p["plan_id"])
    assert (proj.tmdl_dir / "tables" / "Orders.tmdl").exists() and not (proj.tmdl_dir / "tables" / "Sales.tmdl").exists()
    m = Model.load(proj.tmdl_dir)
    assert m.table("Orders") and "ref table Orders" in (proj.tmdl_dir / "model.tmdl").read_text()
    assert "Orders[Amount]" in m.find_measure("Total Sales")[1].node.expr
    assert m.relationships()[0]["from_table"] == "Orders"
    assert checks.validate_project(proj)["ok"]


def test_move_measure_between_tables(proj):
    _, p = run_edit(proj, lambda e: e.move_measure("Sales", "Order Count", "Region"))
    changes.apply(p["plan_id"])
    m = Model.load(proj.tmdl_dir)
    assert m.find_measure("Order Count", "Region") and not m.find_measure("Order Count", "Sales")
    assert checks.validate_project(proj)["ok"]


def test_calculated_column_relationship_role_and_source(proj):
    _, p = run_edit(proj, lambda e: e.upsert_calculated_column("Sales", "Is Large", "Sales[Amount] > 100", data_type="boolean"))
    changes.apply(p["plan_id"])
    assert Model.load(proj.tmdl_dir).table("Sales").node.child("column", "Is Large").expr == "Sales[Amount] > 100"
    _, p = run_edit(proj, lambda e: e.upsert_role("West only", {"Region": 'Region[Region] = "West"'}))
    changes.apply(p["plan_id"])
    roles = Model.load(proj.tmdl_dir).to_dict()["roles"]
    assert roles[0]["name"] == "West only" and "West" in roles[0]["permissions"][0]["filter"]
    _, p = run_edit(proj, lambda e: e.set_partition_source("Region", 'let\n    Source = #table(type table [Region = text, Manager = text], {{"East", "Z"}})\nin\n    Source'))
    changes.apply(p["plan_id"])
    assert '"Z"' in Model.load(proj.tmdl_dir).table("Region").node.child("partition", "Region").props["source"].value
    assert checks.validate_project(proj)["ok"]


def test_relationship_add_delete_set(proj):
    with pytest.raises(edits.EditError):
        edits.Editor(proj).add_relationship("Sales", "Region", "Region", "Region")   # duplicate
    _, p = run_edit(proj, lambda e: e.set_relationship("Sales", "Region", "Region", "Region", cross_filter="bothDirections", active=False))
    changes.apply(p["plan_id"])
    r = Model.load(proj.tmdl_dir).relationships()[0]
    assert r["cross_filter"] == "bothDirections" and r["active"] is False
    _, p = run_edit(proj, lambda e: e.delete_relationship("Sales", "Region", "Region", "Region"))
    changes.apply(p["plan_id"])
    assert Model.load(proj.tmdl_dir).relationships() == []


def test_delete_table_cleans_refs_and_relationships(proj):
    _, p = run_edit(proj, lambda e: e.delete_object("table", "Region"))
    assert p["predicted_new_errors"] == [] or all(e["rule"].startswith("PBIR") for e in p["predicted_new_errors"])
    changes.apply(p["plan_id"])                      # visual uses Region[Region] -> auto rollback expected
    assert Model.load(proj.tmdl_dir).table("Region") is not None


# ------------------------------------------------------------------ report edits
def test_report_edits(proj):
    rep = pbir.Report.load(proj.pbir_dir)
    pid = rep.page_ids()[0]
    ed = edits.Editor(proj)
    new_id, e = pbir.add_page(rep, "Details")
    ed.merge_report_edits(e)
    p = changes.plan(proj, ed.edits(), "add page")
    assert p["predicted_new_errors"] == []
    changes.apply(p["plan_id"])
    rep = pbir.Report.load(proj.pbir_dir)
    assert rep.page(new_id)["displayName"] == "Details" and new_id in rep.page_order()
    vid, e = pbir.add_visual(rep, "Details", "slicer", [{"kind": "Column", "table": "Region", "name": "Region"}], 10, 10, 200, 80, "Region")
    ed = edits.Editor(proj)
    ed.merge_report_edits(e)
    changes.apply(changes.plan(proj, ed.edits(), "add slicer")["plan_id"])
    rep = pbir.Report.load(proj.pbir_dir)
    ed = edits.Editor(proj)
    ed.merge_report_edits(pbir.update_visual(rep, "Details", vid, alt_text="Region filter", hidden=False, width=240))
    changes.apply(changes.plan(proj, ed.edits(), "tweak")["plan_id"])
    rep = pbir.Report.load(proj.pbir_dir)
    s = rep.summary()
    det = next(p for p in s["pages"] if p["display_name"] == "Details")
    assert det["visuals"][0]["alt_text"] == "Region filter" and det["visuals"][0]["width"] == 240
    with pytest.raises(pbir.PbirError):
        pbir.add_visual(rep, "Details", "card", [{"kind": "Column", "table": "Region", "name": "Region", "role": "Bogus"}], 0, 0, 1, 1)
    with pytest.raises(pbir.PbirError):
        pbir.delete_page(pbir.Report.from_texts({"pages/pages.json": "{}", "pages/a/page.json": '{"name":"a"}'}), "a")
    assert pid in rep.page_ids()


def test_theme_edit(proj):
    rep = pbir.Report.load(proj.pbir_dir)
    ed = edits.Editor(proj)
    ed.merge_report_edits(pbir.apply_theme_edit(rep, "Corp", {"name": "Corp", "dataColors": ["#112233", "#445566"]}))
    p = changes.plan(proj, ed.edits(), "theme")
    changes.apply(p["plan_id"])
    assert (proj.report_dir / "StaticResources" / "RegisteredResources" / "Corp.json").exists()
    rj = json.loads((proj.pbir_dir / "report.json").read_text())
    assert rj["themeCollection"]["customTheme"]["name"] == "Corp.json"


# ------------------------------------------------------------------ analysis
def test_bpa_and_unused_and_docs_and_diff(proj, tmp_path):
    res = bpa.run(Model.load(proj.tmdl_dir))
    rules = set(res["by_rule"])
    assert "BPA003" in rules          # Sales[Region] is a visible foreign key
    un = analysis.unused_fields(proj)
    assert un["report_checked"] and any("Order Count" in x["field"] for x in un["used_only_by_other_dax"] + un["unused_measures"])
    assert any(x["field"] == "Sales[OrderId]" for x in un["unused_columns"])
    md = analysis.document(proj)
    assert "## Measures" in md and "```dax" in md and "Inline table" in md
    other = prj.copy_project(str(proj.pbip), str(tmp_path), "Demo3")
    p3 = prj.locate(other["pbip"])
    ed = edits.Editor(p3)
    ed.upsert_measure("Sales", "Extra", "1")
    changes.apply(changes.plan(p3, ed.edits(), "x")["plan_id"])
    diff = analysis.diff_projects(proj, p3)
    assert diff["model"]["measures"] == [{"measure": "Sales[Extra]", "change": "added"}]


# ------------------------------------------------------------------ MCP tool surface
def test_tools_register_and_roundtrip(proj):
    from pbi_mcp import server
    from pbi_mcp.plus import tools
    import asyncio
    listed = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in (listed.tools if hasattr(listed, "tools") else listed)}
    assert {"audit_dashboard", "run_dax_query", "plan_model_change", "apply_change", "upsert_measure_live", "best_practice_scan"} <= names
    assert len(names) >= 54
    ins = tools.inspect_project(str(proj.pbip))
    assert ins["validation"]["ok"] and ins["model"]["counts"]["measures"] == 3 and ins["report"]["pages"][0]["visuals"] == 2
    plan = tools.plan_model_change(str(proj.pbip), "upsert_measure", {"table": "Sales", "name": "Two", "expression": "2"})
    assert plan["status"] == "planned"
    assert tools.apply_change(plan["plan_id"])["status"] == "applied"
    assert tools.read_model_tmdl(str(proj.pbip), table="Sales")["tables"][0]["measures"][-1]["name"] == "Two"
    plan = tools.plan_model_change(str(proj.pbip), "set_properties", {"kind": "measure", "table": "Sales", "name": "Two", "props": {"formatString": "0.0"}})
    tools.apply_change(plan["plan_id"])
    plan = tools.plan_report_change(str(proj.pbip), "create_theme", {"name": "Brand", "colors": ["#0B5FFF", "#00A3A3"]})
    tools.apply_change(plan["plan_id"])
    plan = tools.plan_report_change(str(proj.pbip), "add_visual", {"page": "Overview", "visual_type": "card", "fields": [{"kind": "Measure", "table": "Sales", "name": "Two"}],
                                                                   "x": 10, "y": 500, "width": 200, "height": 100, "title": "Two"})
    assert plan["predicted_new_errors"] == []
    tools.apply_change(plan["plan_id"])
    assert tools.validate_project(str(proj.pbip))["ok"]
    with pytest.raises(ValueError, match="Unknown operation"):
        tools.plan_model_change(str(proj.pbip), "drop_database", {})
    with pytest.raises(ValueError, match="Bad parameters"):
        tools.plan_model_change(str(proj.pbip), "upsert_measure", {"table": "Sales"})
    md = tools.document_project(str(proj.pbip), output_path=str(proj.root / "doc.md"))
    assert Path(md["saved_to"]).exists()
    with pytest.raises(ValueError, match="outside"):
        tools.document_project(str(proj.pbip), output_path=str(proj.model_dir / "x.md"))


def test_analyze_project_queries_offline(proj):
    from pbi_mcp.plus import tools
    # point Region at a SQL Server source with native SQL so the SQL/connection analyzers have something to say
    m = 'let\n    Source = Sql.Database("prodsql01", "Sales"),\n    Q = Value.NativeQuery(Source, "SELECT * FROM dbo.Region WITH (NOLOCK)", null, [EnableFolding=false])\nin\n    Q'
    plan = tools.plan_model_change(str(proj.pbip), "set_partition_source", {"table": "Region", "m_expression": m})
    tools.apply_change(plan["plan_id"])
    out = tools.analyze_project_queries(str(proj.pbip))
    assert out["query_count"] == 2 and "error" not in out["sql"] and "error" not in out["connections"]
    sql_rules = " ".join(str(f) for f in out["sql"]["findings"])
    assert "NOLOCK" in sql_rules.upper() or "select" in sql_rules.lower()
    with pytest.raises(ValueError):
        tools.analyze_project_queries(str(proj.pbip), "bogus")


# ------------------------------------------------------------------ security regressions
def test_restore_rejects_traversal_and_untrusted_manifest(proj, tmp_path):
    for bad in ("D:/evil", "../x/y", "a/../b", "a", "a/b/c", "C:/x/y"):
        with pytest.raises(changes.ChangeError):
            changes.restore(bad)
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Temp", "1"))
    res = changes.apply(p["plan_id"])
    key, stamp = res["backup_id"].split("/")
    mf = changes.backup_root() / key / stamp / "manifest.json"
    m = json.loads(mf.read_text())
    m["files"][0]["rel"] = "../../../outside.txt"
    mf.write_text(json.dumps(m))
    with pytest.raises(changes.ChangeError, match="unsafe"):
        changes.restore(res["backup_id"], confirm=True)
    m["files"][0]["rel"] = "C:/Windows/x.txt"
    mf.write_text(json.dumps(m))
    with pytest.raises(changes.ChangeError, match="unsafe"):
        changes.restore(res["backup_id"], confirm=True)


def test_restore_previews_until_confirmed(proj):
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Temp", "1"))
    res = changes.apply(p["plan_id"])
    prev = changes.restore(res["backup_id"])
    assert prev["status"] == "preview" and Model.load(proj.tmdl_dir).find_measure("Temp")


def test_line_breaks_in_values_and_names_rejected(proj):
    with pytest.raises(ValueError):
        edits.Editor(proj).upsert_measure("Sales", "Bad", "1", format_string="0\n\nrole Admin\n\tmodelPermission: administrator")
    with pytest.raises(ValueError):
        edits.Editor(proj).rename("measure", "Sales", "Total Sales", "X\nrole Y")
    with pytest.raises(ValueError):
        edits.Editor(proj).upsert_role("A\nB", {"Region": "TRUE()"})
    with pytest.raises(edits.EditError):
        edits.Editor(proj).add_relationship("Sales", "Region", "Region", "Manager", from_cardinality="many\nfoo: bar")
    with pytest.raises(edits.EditError):
        edits.Editor(proj).set_properties("column", "Sales", "Amount", {"summarizeBy": "sum\nx: y"})


def test_object_names_cannot_choose_file_paths(proj):
    ed = edits.Editor(proj)
    ed.upsert_role("../model", {"Region": "TRUE()"})
    assert all(".." not in str(p) for p in ed.edits())
    assert (proj.tmdl_dir / "roles" / "___model.tmdl").resolve() in ed.edits()
    ed2 = edits.Editor(proj)
    ed2.rename("table", "Sales", None, "../model")
    assert all(p.resolve().is_relative_to(proj.root.resolve()) for p in ed2.edits())
    assert not any(p.name == "model.tmdl" and p.parent == proj.tmdl_dir and ed2.edits()[p] is None for p in ed2.edits())


def test_write_gates_cover_create_copy_and_documents(proj, tmp_path, monkeypatch):
    from pbi_mcp.plus import tools
    monkeypatch.setenv("PBI_MCP_READONLY", "1")
    with pytest.raises(prj.ProjectError, match="disabled"):
        tools.create_project(str(tmp_path), "New1")
    with pytest.raises(prj.ProjectError, match="disabled"):
        tools.copy_project(str(proj.pbip), str(tmp_path), "Copy1")
    with pytest.raises(changes.ChangeError):
        tools.document_project(str(proj.pbip), output_path=str(proj.root / "d.md"))
    monkeypatch.delenv("PBI_MCP_READONLY")
    monkeypatch.setenv("PBI_MCP_EDIT_ROOT", str(proj.root))
    with pytest.raises(prj.ProjectError, match="restricted"):
        tools.create_project(str(tmp_path), "New2")
    monkeypatch.delenv("PBI_MCP_EDIT_ROOT")
    for bad in ("NUL", "con.txt", "a/b", "..", "x.", " x"):
        with pytest.raises(prj.ProjectError):
            prj.check_folder_name(bad)
    with pytest.raises(ValueError, match="inside the project"):
        tools.document_project(str(proj.pbip), output_path=str(tmp_path / "elsewhere.md"))
    with pytest.raises(ValueError):
        tools.document_project(str(proj.pbip), output_path=str(proj.root / "CLAUDE.md"))
    with pytest.raises(ValueError):
        tools.document_project(str(proj.pbip), output_path=str(proj.root / ".claude" / "x.md"))


def test_live_confirm_token_required():
    from pbi_mcp.plus import live
    with pytest.raises(live.LiveError, match="not previewed"):
        live._require("abc", None)
    k = live._issue("k1")
    live._require("k1", k)
    with pytest.raises(live.LiveError):
        live._require("k1", k)                 # single use
    assert live._dax_measure("a]b") == "[a]]b]" and live._dax_table("O'Neil") == "'O''Neil'"


def test_source_targets_are_redacted(proj):
    from pbi_mcp.plus import tools
    m = 'let\n    Source = Web.Contents("https://user:pw@api.example.com/v1/data?api_key=SECRET123")\nin\n    Source'
    plan = tools.plan_model_change(str(proj.pbip), "set_partition_source", {"table": "Region", "m_expression": m})
    tools.apply_change(plan["plan_id"])
    out = json.dumps(tools.classify_sources_project(str(proj.pbip)))
    assert "SECRET123" not in out and "pw@" not in out
    assert "SECRET123" not in tools.document_project(str(proj.pbip))["markdown"].split("```dax")[0].split("## Tables")[0]


# ------------------------------------------------------------------ code-review regressions
def test_role_upsert_cannot_clobber_other_roles_or_drop_content(proj):
    _, p = run_edit(proj, lambda e: e.upsert_role("Admin_", {"Region": "TRUE()"}))
    changes.apply(p["plan_id"])
    with pytest.raises(edits.EditError, match="already exists"):
        edits.Editor(proj).upsert_role("Admin?", {"Region": "TRUE()"})          # same sanitized file name, different role
    f = proj.tmdl_dir / "roles" / "Admin_.tmdl"
    f.write_text(f.read_text() + "\tannotation Owner = HR\n\tcolumnPermission Region.Manager = none\n", encoding="utf-8")
    _, p = run_edit(proj, lambda e: e.upsert_role("Admin_", {"Sales": "Sales[Amount] > 0"}))
    changes.apply(p["plan_id"])
    text = f.read_text()
    assert "annotation Owner" in text and "columnPermission" in text and "tablePermission Region = TRUE()" in text and "tablePermission Sales" in text
    _, p = run_edit(proj, lambda e: e.upsert_role("admin_", {"Region": "FALSE()"}))     # case-insensitive match updates in place
    changes.apply(p["plan_id"])
    assert "FALSE()" in f.read_text() and "Sales[Amount] > 0" in f.read_text()


def test_table_rename_rewrites_bare_table_arguments(proj):
    _, p = run_edit(proj, lambda e: e.rename("table", "Sales", None, "Orders"))
    changes.apply(p["plan_id"])
    m = Model.load(proj.tmdl_dir)
    assert m.find_measure("Order Count")[1].node.expr == "COUNTROWS(Orders)"
    assert checks.validate_project(proj)["ok"]
    e, n = daxrefs.rename_in_expr('SUMX(Sales, Sales[A]) + ALL(Sales) + [Sales] + SalesTotal(1) + "Sales"', kind="table", table=None,
                                  old="Sales", new="Orders", owner_table=None)
    assert e == 'SUMX(Orders, Orders[A]) + ALL(Orders) + [Sales] + SalesTotal(1) + "Sales"' and n == 3


def test_rename_reaches_fenced_expressions(proj):
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    t = f.read_text()
    t = t.replace("measure 'Average Order' = DIVIDE([Total Sales], [Order Count])",
                  "measure 'Average Order' = ```\n\t\t\tDIVIDE([Total Sales], [Order Count])\n\t\t\t```")
    f.write_text(t, encoding="utf-8")
    _, p = run_edit(proj, lambda e: e.rename("measure", "Sales", "Total Sales", "Revenue"))
    assert p["predicted_new_errors"] == []
    changes.apply(p["plan_id"])
    assert "[Revenue]" in f.read_text() and "[Total Sales]" not in f.read_text()


def test_gate_blocks_dangling_references(proj):
    _, p = run_edit(proj, lambda e: e.delete_object("column", "Sales", "Amount"))          # Total Sales sums it
    assert any(e["rule"] in ("DAX103", "DAX102") for e in p["predicted_new_errors"])
    assert changes.apply(p["plan_id"])["status"] == "rolled_back_validation_failed"
    assert Model.load(proj.tmdl_dir).table("Sales").node.child("column", "Amount")
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    f.write_text(f.read_text() + "\n\tcolumn Calc = Region[Nope] * 2\n\t\tdataType: double\n", encoding="utf-8")
    assert any(x["rule"] == "DAX103" for x in checks.validate_project(proj)["findings"])


def test_multiple_pbip_in_one_folder_still_apply_and_restore(proj):
    other = proj.root / "Other.pbip"
    other.write_text(proj.pbip.read_text(), encoding="utf-8")
    p0 = prj.locate(str(proj.pbip))
    ed = edits.Editor(p0)
    ed.upsert_measure("Sales", "Two", "2")
    plan = changes.plan(p0, ed.edits(), "x")
    assert plan["predicted_new_errors"] == []
    res = changes.apply(plan["plan_id"])
    assert res["status"] == "applied"
    assert changes.restore(res["backup_id"], confirm=True)["status"] == "restored"


def test_rollback_reports_failures_and_continues(proj):
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Temp", "1"))
    res = changes.apply(p["plan_id"])
    key, stamp = res["backup_id"].split("/")
    bdir = changes.backup_root() / key / stamp
    m = json.loads((bdir / "manifest.json").read_text())
    m["files"].append({**m["files"][0], "rel": "../escape.txt"})
    failures = changes._restore_files(m, bdir)
    assert len(failures) == 1 and "unsafe" in failures[0]
    assert "Temp" not in (proj.tmdl_dir / "tables" / "Sales.tmdl").read_text()          # the valid entry was still restored


def test_crlf_files_keep_their_line_endings(proj):
    f = proj.tmdl_dir / "tables" / "Sales.tmdl"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    _, p = run_edit(proj, lambda e: e.upsert_measure("Sales", "Two", "2"))
    assert p["diff"].count("\n+") < 12                                                   # only the new measure, not the whole file
    changes.apply(p["plan_id"])
    raw = f.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") and b"Two" in raw


def test_mask_handles_quotes_in_comments_and_names():
    e = '// 5" screens\n[Total] + LEN("x") + Sales[Col "q"]'
    assert [(r.table, r.name) for r in daxrefs.refs(e)] == [(None, "Total"), ("Sales", 'Col "q"')]
    assert daxrefs.balanced(e) == []
    assert daxrefs.balanced('"abc') == ["unterminated string literal"]
    assert "unterminated /* comment" in daxrefs.balanced("1 /* x")


def test_case_insensitive_name_rules(proj):
    with pytest.raises(edits.EditError):
        edits.Editor(proj).upsert_measure("Sales", "amount", "1")                         # column Amount exists
    ed = edits.Editor(proj)
    ed.upsert_measure("Sales", "total sales", "SUM(Sales[Amount]) + 0")                   # updates, not duplicates
    assert len(ed.docs["tables/Sales.tmdl"].nodes[0].kids("measure")) == 3
    ed = edits.Editor(proj)
    ed.rename("measure", "Sales", "Order Count", "ORDER COUNT")                           # case-only rename allowed
    assert ed.model.find_measure("Order Count")


def test_measure_rename_skips_columns_of_same_name_in_own_table():
    e, n = daxrefs.rename_in_expr("SUMX(Region, [Amount]) + [Amount]", kind="measure", table="Sales", old="Amount", new="Amt",
                                  owner_table="Region", col_index={"region": {"amount"}, "sales": set()})
    assert n == 0
    e, n = daxrefs.rename_in_expr("[Amount]", kind="measure", table="Sales", old="Amount", new="Amt", owner_table="Sales", col_index={"sales": set()})
    assert e == "[Amt]" and n == 1


def test_report_rename_updates_aggregate_refs_and_scopes_native_ref():
    doc = {"query": {"queryState": {"Y": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amt"}}, "queryRef": "Sum(Sales.Amt)", "nativeQueryRef": "Amt"},
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Other"}}, "Property": "Amt"}}, "queryRef": "Other.Amt", "nativeQueryRef": "Amt"}]}}},
        "objects": {"dataPoint": [{"selector": {"metadata": "Sum(Sales.Amt)"}}]}, "title": {"displayName": "Sales.Overview"}}
    rep = pbir.Report.from_texts({"pages/p/visuals/v/visual.json": json.dumps(doc)})
    out = json.loads(pbir.rename_field_refs(rep, "column", "Sales", "Amt", "Net")["pages/p/visuals/v/visual.json"])
    p0, p1 = out["query"]["queryState"]["Y"]["projections"]
    assert p0["queryRef"] == "Sum(Sales.Net)" and p0["nativeQueryRef"] == "Net" and p1["nativeQueryRef"] == "Amt" and p1["queryRef"] == "Other.Amt"
    assert out["objects"]["dataPoint"][0]["selector"]["metadata"] == "Sum(Sales.Net)"
    out = json.loads(pbir.rename_field_refs(rep, "table", None, "Sales", "Orders")["pages/p/visuals/v/visual.json"])
    assert out["title"]["displayName"] == "Sales.Overview"


def test_live_format_helpers():
    from pbi_mcp.plus import live
    assert live._format_of({"FormatString": "0.0"}) == "0.0"
    assert live._format_of({"FormatString": None, "FormatStringDefinition": '"#,0"'}) == "#,0"
    assert live._format_of({"FormatString": None, "FormatStringDefinition": 'IF([x]>1,"a","b")'}) is None
    assert live._dynamic_format({"FormatString": None, "FormatStringDefinition": 'IF([x]>1,"a","b")'})


# ------------------------------------------------------------------ multi-client packaging
def test_client_configs_are_valid_json_and_name_the_server():
    root = Path(__file__).resolve().parents[1]
    ext = json.loads((root / "gemini-extension.json").read_text(encoding="utf-8"))
    assert "powerbi-desktop" in ext["mcpServers"] and ext["contextFileName"] == "GEMINI.md"
    for name, key in (("vscode-mcp.json", "servers"), ("copilot-cli-mcp-config.json", "mcpServers"), ("gemini-settings.json", "mcpServers")):
        cfg = json.loads((root / "docs" / "clients" / name).read_text(encoding="utf-8"))
        assert cfg[key]["powerbi-desktop"]["command"] == "powerbi-desktop-mcp"
    for f in ("AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md"):
        assert (root / f).is_file()


def test_install_skills_copies_every_skill(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import install_skills
    for target in ("claude", "gemini", "copilot"):
        lines = install_skills.install(target, str(tmp_path / target))
        assert len(lines) == 11
    assert (tmp_path / "copilot" / ".github" / "skills" / "powerbi" / "SKILL.md").is_file()
    assert (tmp_path / "gemini" / ".gemini" / "skills" / "POWERBI-REGISTRY.md").is_file()
    assert install_skills.install("claude", str(tmp_path / "dry"), dry_run=True) and not (tmp_path / "dry").exists()


def test_every_skill_has_frontmatter_and_is_short():
    root = Path(__file__).resolve().parents[1]
    for d in (root / "skills").iterdir():
        if d.is_dir():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            assert text.startswith("---") and f"name: {d.name}" in text.split("---")[1], d.name
            assert len(text.splitlines()) < 500, d.name


def test_server_exposes_all_tools_over_the_registry():
    import asyncio
    from pbi_mcp import server
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(names) == len(set(names)) == 58
    assert {"audit_dashboard", "propose_change", "plan_model_change", "upsert_measure_live"} <= set(names)
