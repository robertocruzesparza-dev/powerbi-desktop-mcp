"""Create a new, self-contained Power BI project (TMDL model + PBIR report) from scratch.

The sample data is an inline M table, so the project opens and refreshes with no credentials,
no gateway and no external source. Replace the tables in Desktop (or with the edit tools) to
point at real data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import pbir
from .project import ProjectError, locate


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def create_project(parent: str, name: str, with_sample: bool = True) -> dict[str, Any]:
    base = Path(parent).expanduser().resolve()
    if not base.is_dir():
        raise ProjectError(f"Parent folder does not exist: {base}")
    from . import changes
    from .project import check_folder_name
    check_folder_name(name)
    if "." in name:
        raise ProjectError("name must not contain dots (they confuse the .Report/.SemanticModel naming).")
    root = base / name
    try:
        changes.check_write_target(root)
    except changes.ChangeError as exc:
        raise ProjectError(str(exc)) from exc
    if root.exists():
        raise ProjectError(f"{root} already exists; nothing was created.")
    model = root / f"{name}.SemanticModel"
    report = root / f"{name}.Report"
    _w(root / f"{name}.pbip", _j({"version": "1.0", "artifacts": [{"report": {"path": f"{name}.Report"}}], "settings": {"enableAutoRecovery": True}}))
    _w(root / ".gitignore", "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n")
    _w(model / "definition.pbism", _j({"version": "4.0", "settings": {}}))
    d = model / "definition"
    _w(d / "database.tmdl", "database\n\tcompatibilityLevel: 1600\n")
    tables = []
    if with_sample:
        tables = ["Sales", "Region"]
        _w(d / "tables" / "Region.tmdl", _REGION)
        _w(d / "tables" / "Sales.tmdl", _SALES)
        _w(d / "relationships.tmdl", "relationship 5b0f6d8e-3b1a-4a55-9d57-1c3f0e9a7a01\n\tfromColumn: Sales.Region\n\ttoColumn: Region.Region\n")
    refs = "".join(f"ref table {t}\n" for t in tables)
    _w(d / "model.tmdl", "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\tsourceQueryCulture: en-US\n\n" + refs)
    _w(report / "definition.pbir", _j({"$schema": f"https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                                      "version": "4.0", "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}}))
    rd = report / "definition"
    page_id = pbir.new_id()
    _w(rd / "version.json", _j({"$schema": pbir.FALLBACK_SCHEMA["version"], "version": "2.0.0"}))
    _w(rd / "report.json", _j({"$schema": pbir.FALLBACK_SCHEMA["report"],
                               "themeCollection": {"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": "5.59", "type": "SharedResources"}},
                               "layoutOptimization": "None", "settings": {"useStylableVisualContainerHeader": True}}))
    _w(rd / "pages" / "pages.json", _j({"$schema": pbir.FALLBACK_SCHEMA["pages"], "pageOrder": [page_id], "activePageName": page_id}))
    _w(rd / "pages" / page_id / "page.json", _j({"$schema": pbir.FALLBACK_SCHEMA["page"], "name": page_id, "displayName": "Overview",
                                               "displayOption": "FitToPage", "height": 720, "width": 1280}))
    if with_sample:
        rep = pbir.Report.load(rd)
        _, e1 = pbir.add_visual(rep, page_id, "card", [{"kind": "Measure", "table": "Sales", "name": "Total Sales"}], 40, 40, 240, 120, "Total Sales")
        for rel, text in e1.items():
            _w(rd / rel, text)
        rep = pbir.Report.load(rd)
        _, e2 = pbir.add_visual(rep, page_id, "clusteredBarChart",
                                [{"kind": "Column", "table": "Region", "name": "Region"}, {"kind": "Measure", "table": "Sales", "name": "Total Sales"}],
                                320, 40, 600, 360, "Sales by region")
        for rel, text in e2.items():
            _w(rd / rel, text)
    return locate(str(root / f"{name}.pbip")).describe()


_REGION = """table Region
	lineageTag: 0a7c4d2e-6f31-4c1a-8f10-6a5d1d9c0b11

	column Region
		dataType: string
		lineageTag: 2f0b9a1c-4d55-4e6b-9a63-0c2b8d7e1f22
		summarizeBy: none
		sourceColumn: Region

	column Manager
		dataType: string
		lineageTag: 7c1e3b5d-8a24-4f90-b6d1-3e5a9c0d2f33
		summarizeBy: none
		sourceColumn: Manager

	partition Region = m
		mode: import
		source =
				let
				    Source = #table(type table [Region = text, Manager = text], {{"East", "Avery"}, {"West", "Blake"}, {"North", "Casey"}})
				in
				    Source
"""

_SALES = """table Sales
	lineageTag: 4b8d1f6a-2c93-4d7e-a1b5-9e0f3c6d8a44

	measure 'Total Sales' = SUM(Sales[Amount])
		formatString: #,0
		lineageTag: 9d2e5a7c-1b48-4c36-8e0f-5a3d7c1b9e55

	measure 'Order Count' = COUNTROWS(Sales)
		formatString: 0
		lineageTag: 1e6b3d9f-5a27-4b84-9c10-7d2f4a8e0b66

	measure 'Average Order' = DIVIDE([Total Sales], [Order Count])
		formatString: #,0.00
		lineageTag: 6a0c4e8b-3d15-4f79-b2a6-1c9e5d7f3a77

	column OrderId
		dataType: int64
		formatString: 0
		lineageTag: 3c7a1e5b-9f42-4d08-a6b3-8e1d5c9f2a88
		summarizeBy: none
		sourceColumn: OrderId

	column Region
		dataType: string
		lineageTag: 8f2d6b0a-4e19-4c57-9d31-2a7c0e5b8f99
		summarizeBy: none
		sourceColumn: Region

	column Amount
		dataType: double
		lineageTag: 5d9b3f7e-1a68-4b20-8c45-6e0a2d4f7b00
		summarizeBy: sum
		sourceColumn: Amount

	partition Sales = m
		mode: import
		source =
				let
				    Source = #table(type table [OrderId = Int64.Type, Region = text, Amount = number], {{1, "East", 120.5}, {2, "West", 80}, {3, "North", 45.25}, {4, "East", 200}, {5, "West", 150}})
				in
				    Source
"""
