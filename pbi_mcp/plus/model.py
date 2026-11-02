"""Semantic model loaded from a project's TMDL folder: queryable, JSON-friendly, editable by span."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tmdl
from .daxrefs import refs as dax_refs
from .tmdl import Doc, Node


@dataclass
class Located:
    file: str          # posix path relative to the definition folder
    node: Node


@dataclass
class Model:
    root: Path
    docs: dict[str, Doc] = field(default_factory=dict)
    parse_errors: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, definition_dir: Path) -> "Model":
        m = cls(root=definition_dir)
        for path in sorted(definition_dir.rglob("*.tmdl")):
            rel = path.relative_to(definition_dir).as_posix()
            try:
                m.docs[rel] = tmdl.parse(path.read_bytes().decode("utf-8-sig"))     # bytes: keep CRLF line endings
            except (tmdl.TmdlError, UnicodeDecodeError) as exc:
                m.parse_errors[rel] = str(exc)
        return m

    @classmethod
    def from_texts(cls, texts: dict[str, str]) -> "Model":
        m = cls(root=Path("."))
        for rel, text in texts.items():
            if rel.endswith(".tmdl"):
                try:
                    m.docs[rel] = tmdl.parse(text)
                except tmdl.TmdlError as exc:
                    m.parse_errors[rel] = str(exc)
        return m

    # ---------------------------------------------------------------- access
    def _top(self, kind: str) -> list[Located]:
        return [Located(f, n) for f, d in self.docs.items() for n in d.nodes if n.kind == kind]

    def tables(self) -> list[Located]:
        return self._top("table")

    def table(self, name: str) -> Located | None:
        for t in self.tables():
            if t.node.name.lower() == name.lower():
                return t
        return None

    def measures(self) -> list[tuple[str, Located]]:
        return [(t.node.name, Located(t.file, c)) for t in self.tables() for c in t.node.kids("measure")]

    def find_measure(self, name: str, table: str | None = None) -> tuple[str, Located] | None:
        for tname, loc in self.measures():
            if loc.node.name.lower() == name.lower() and (table is None or tname.lower() == table.lower()):
                return tname, loc
        return None

    def columns(self) -> list[tuple[str, Located]]:
        return [(t.node.name, Located(t.file, c)) for t in self.tables() for c in t.node.kids("column")]

    def relationships(self) -> list[dict[str, Any]]:
        out = []
        for loc in self._top("relationship"):
            n = loc.node
            frm = tmdl.split_dotted(n.prop("fromColumn") or "")
            to = tmdl.split_dotted(n.prop("toColumn") or "")
            out.append({
                "id": n.name, "file": loc.file, "line": n.start + 1,
                "from_table": frm[0] if frm else None, "from_column": frm[1] if len(frm) > 1 else None,
                "to_table": to[0] if to else None, "to_column": to[1] if len(to) > 1 else None,
                "active": (n.prop("isActive") or "true").lower() != "false",
                "cross_filter": (n.prop("crossFilteringBehavior") or "oneDirection"),
                "from_cardinality": n.prop("fromCardinality") or "many",
                "to_cardinality": n.prop("toCardinality") or "one",
                "join_on_date_behavior": n.prop("joinOnDateBehavior"),
            })
        return out

    def roles(self) -> list[Located]:
        return self._top("role")

    def shared_expressions(self) -> list[Located]:
        return self._top("expression")

    def functions(self) -> list[Located]:
        return self._top("function")

    def model_node(self) -> Node | None:
        ms = self._top("model")
        return ms[0].node if ms else None

    # ---------------------------------------------------------------- summaries
    def column_info(self, c: Node) -> dict[str, Any]:
        return {
            "name": c.name, "data_type": c.prop("dataType"), "hidden": c.flag("isHidden"),
            "is_key": c.flag("isKey"), "calculated": c.expr is not None,
            "expression": c.expr, "format": c.prop("formatString"), "summarize_by": c.prop("summarizeBy"),
            "sort_by": c.prop("sortByColumn"), "display_folder": c.prop("displayFolder"),
            "source_column": c.prop("sourceColumn"), "data_category": c.prop("dataCategory"),
            "description": c.description, "line": c.start + 1,
        }

    def measure_info(self, c: Node) -> dict[str, Any]:
        return {
            "name": c.name, "expression": c.expr or "", "format": c.prop("formatString"),
            "hidden": c.flag("isHidden"), "display_folder": c.prop("displayFolder"),
            "description": c.description, "line": c.start + 1, "lineage_tag": c.prop("lineageTag"),
        }

    def to_dict(self, include_expressions: bool = True) -> dict[str, Any]:
        tables = []
        for t in self.tables():
            n = t.node
            parts = [{"name": p.name, "type": p.expr, "mode": p.prop("mode"),
                      "source": (p.props["source"].value if "source" in p.props else None)} for p in n.kids("partition")]
            calc = any((p["type"] or "").strip() == "calculated" for p in parts)
            tables.append({
                "name": n.name, "file": t.file, "hidden": n.flag("isHidden"), "calculated": calc or n.expr is not None,
                "data_category": n.prop("dataCategory"), "description": n.description,
                "columns": [self.column_info(c) for c in n.kids("column")],
                "measures": [self.measure_info(c) for c in n.kids("measure")],
                "hierarchies": [{"name": h.name, "levels": [lv.name for lv in h.kids("level")]} for h in n.kids("hierarchy")],
                "partitions": parts if include_expressions else [{"name": p["name"], "mode": p["mode"]} for p in parts],
            })
        model = self.model_node()
        return {
            "model": {"name": model.name if model else None, "culture": model.prop("culture") if model else None,
                      "default_power_bi_data_source_version": model.prop("defaultPowerBIDataSourceVersion") if model else None},
            "tables": tables, "relationships": self.relationships(),
            "roles": [{"name": r.node.name, "permissions": [{"table": tp.name, "filter": tp.prop("filterExpression") or tp.expr}
                                                           for tp in r.node.kids("tablePermission")]} for r in self.roles()],
            "expressions": [{"name": e.node.name, "expression": e.node.expr if include_expressions else None} for e in self.shared_expressions()],
            "functions": [{"name": f.node.name, "expression": f.node.expr if include_expressions else None} for f in self.functions()],
            "parse_errors": self.parse_errors,
            "counts": {"tables": len(tables), "measures": sum(len(t["measures"]) for t in tables),
                       "columns": sum(len(t["columns"]) for t in tables), "relationships": len(self.relationships())},
        }

    # ---------------------------------------------------------------- dependency graph
    def measure_graph(self) -> dict[str, Any]:
        """Which measures reference which measures/columns, and which measures nothing references."""
        names = {loc.node.name.lower(): (t, loc.node.name) for t, loc in self.measures()}
        cols = {(t.node.name.lower(), c.name.lower()) for t in self.tables() for c in t.node.kids("column")}
        edges: dict[str, dict[str, list[str]]] = {}
        referenced: set[str] = set()
        unresolved: list[dict[str, str]] = []
        for tname, loc in self.measures():
            key = f"{tname}[{loc.node.name}]"
            mrefs: list[str] = []
            crefs: list[str] = []
            for r in dax_refs(loc.node.expr or ""):
                if r.table is None:
                    if r.name.lower() in names:
                        t2, n2 = names[r.name.lower()]
                        mrefs.append(f"{t2}[{n2}]")
                        referenced.add(f"{t2}[{n2}]")
                    elif (tname.lower(), r.name.lower()) in cols:
                        crefs.append(f"{tname}[{r.name}]")
                    else:
                        unresolved.append({"in": key, "ref": f"[{r.name}]"})
                else:
                    if (r.table.lower(), r.name.lower()) in cols:
                        crefs.append(f"{r.table}[{r.name}]")
                    elif r.name.lower() in names and names[r.name.lower()][0].lower() == r.table.lower():
                        t2, n2 = names[r.name.lower()]
                        mrefs.append(f"{t2}[{n2}]")
                        referenced.add(f"{t2}[{n2}]")
                    else:
                        unresolved.append({"in": key, "ref": f"{r.table}[{r.name}]"})
            edges[key] = {"measures": sorted(set(mrefs)), "columns": sorted(set(crefs))}
        cycles = _find_cycles({k: v["measures"] for k, v in edges.items()})
        return {"edges": edges, "referenced_measures": sorted(referenced),
                "unreferenced_by_other_measures": sorted(set(edges) - referenced),
                "unresolved_references": unresolved, "cycles": cycles}

    def unresolved_dax(self) -> list[dict[str, Any]]:
        """References in ANY DAX body (measures, calculated columns/tables, RLS, functions) that resolve to nothing.
        qualified=True means Table[Name] named a missing table or field (certain); False is a bare [Name] (could be a virtual column)."""
        cols = {t.node.name.lower(): {c.name.lower() for c in t.node.kids("column")} for t in self.tables()}
        meas = {loc.node.name.lower() for _, loc in self.measures()}
        out: list[dict[str, Any]] = []
        for label, owner, expr in self.all_dax():
            variables = {v.lower() for v in re.findall(r"\bVAR\s+([A-Za-z_]\w*)", expr, re.I)}
            for r in dax_refs(expr):
                if r.table is not None:
                    if r.table.lower() in variables:
                        continue
                    tc = cols.get(r.table.lower())
                    if tc is None:
                        out.append({"in": label, "ref": f"{r.table}[{r.name}]", "qualified": True, "why": f"table {r.table!r} not found"})
                    elif r.name.lower() not in tc and r.name.lower() not in meas:
                        out.append({"in": label, "ref": f"{r.table}[{r.name}]", "qualified": True, "why": f"no column or measure {r.name!r} in {r.table!r}"})
                elif r.name.lower() not in meas and r.name.lower() not in cols.get((owner or "").lower(), set()):
                    out.append({"in": label, "ref": f"[{r.name}]", "qualified": False, "why": "no measure or column of that name"})
        return out

    def all_dax(self) -> list[tuple[str, str, str]]:
        """(location label, owner table, expression) for every DAX body in the model."""
        out: list[tuple[str, str, str]] = []
        for t in self.tables():
            for m in t.node.kids("measure"):
                out.append((f"{t.node.name}[{m.name}]", t.node.name, m.expr or ""))
            for c in t.node.kids("column"):
                if c.expr:
                    out.append((f"{t.node.name}[{c.name}] (calculated column)", t.node.name, c.expr))
            for p in t.node.kids("partition"):
                if (p.expr or "").strip() == "calculated" and "source" in p.props:
                    out.append((f"{t.node.name} (calculated table)", t.node.name, p.props["source"].value))
        for r in self.roles():
            for tp in r.node.kids("tablePermission"):
                f = tp.prop("filterExpression") or tp.expr
                if f:
                    out.append((f"role {r.node.name} / {tp.name}", tp.name, f))
        for f in self.functions():
            out.append((f"function {f.node.name}", "", f.node.expr or ""))
        return out


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> None:
        state[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if state.get(m) == 1:
                cycles.append(stack[stack.index(m):] + [m])
            elif state.get(m) is None:
                visit(m)
        stack.pop()
        state[n] = 2

    for node in graph:
        if state.get(node) is None:
            visit(node)
    return cycles


class _N:
    """Minimal Node stand-in for models that did not come from TMDL (live snapshots)."""
    def __init__(self, name: str, expr: str, hidden: bool):
        self.name, self.expr, self._hidden = name, expr, hidden

    def flag(self, key: str) -> bool:
        return self._hidden if key == "isHidden" else False


class DictModel(Model):
    """A Model backed by a to_dict()-shaped snapshot (used for open Desktop files)."""

    def __init__(self, data: dict[str, Any]):
        super().__init__(root=Path("."))
        self._data = data

    def to_dict(self, include_expressions: bool = True) -> dict[str, Any]:
        return self._data

    def measures(self):
        return [(t["name"], Located("", _N(m["name"], m["expression"], m["hidden"]))) for t in self._data["tables"] for m in t["measures"]]

    def tables(self):
        class _T:
            def __init__(self, t):
                self.file = ""
                self.node = type("TN", (), {"name": t["name"], "kids": lambda s, k, t=t: [type("C", (), {"name": c["name"]})() for c in t["columns"]] if k == "column" else []})()
        return [_T(t) for t in self._data["tables"]]

    def columns(self):
        return [(t["name"], Located("", _N(c["name"], c.get("expression") or "", c["hidden"]))) for t in self._data["tables"] for c in t["columns"]]

    def relationships(self):
        return self._data["relationships"]

    def roles(self):
        return []
