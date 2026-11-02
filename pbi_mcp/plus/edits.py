"""High-level model and report edits. Each Editor method only mutates in-memory text; call
`edits()` to get {absolute path: new text or None} for `changes.plan`."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from . import daxrefs, pbir, tmdl
from .model import Model
from .project import Project
from .tmdl import Doc, Node, quote_name


class EditError(ValueError):
    pass


_DATA_TYPES = {"string", "int64", "double", "decimal", "dateTime", "boolean", "binary", "variant", "automatic", "unknown"}
_SUMMARIZE = {"none", "sum", "min", "max", "count", "average", "distinctCount", "default"}
_CARDINALITY = {"one", "many"}


def _enum(value: str | None, allowed: set[str], what: str) -> str | None:
    if value is not None and value not in allowed:
        raise EditError(f"{what} must be one of {sorted(allowed)}, got {value!r}.")
    return value


def _safe_file(name: str) -> str:
    """File stem for an object name: letters, digits, space, dash, underscore only."""
    import re as _re
    stem = _re.sub(r"[^\w \-]", "_", name).strip(" .")
    if not stem:
        raise EditError("The name cannot be turned into a file name.")
    return stem


_SIMPLE_PROPS = {"formatString", "displayFolder", "dataCategory", "summarizeBy", "sortByColumn", "dataType", "description"}


class Editor:
    def __init__(self, project: Project):
        if project.tmdl_dir is None:
            raise EditError("This project's semantic model is not in TMDL (definition folder missing), so model edits are not supported. "
                            "In Power BI Desktop use File > Save as, choose Power BI Project, and enable TMDL in Options > Preview features.")
        self.project = project
        self.root = project.tmdl_dir
        self.model = Model.load(project.tmdl_dir)
        if self.model.parse_errors:
            raise EditError(f"Model files that cannot be parsed block editing: {self.model.parse_errors}")
        self.docs: dict[str, Doc] = dict(self.model.docs)
        self.original: dict[str, str] = {rel: d.to_text() for rel, d in self.docs.items()}
        self.created: dict[str, str] = {}
        self.deleted: set[str] = set()
        self.report_edits: dict[str, str | None] = {}

    # ------------------------------------------------------------------ lookups
    def _table(self, name: str) -> tuple[str, Node]:
        for rel, d in self.docs.items():
            for n in d.nodes:
                if n.kind == "table" and n.name.lower() == name.lower():
                    return rel, n
        raise EditError(f"Table {name!r} not found. Tables: " + ", ".join(sorted(t.node.name for t in self._live_tables())))

    def _live_tables(self):
        return [type("T", (), {"node": n}) for d in self.docs.values() for n in d.nodes if n.kind == "table"]

    def _refind(self, rel: str, kind: str, name: str, parent: str | None = None) -> Node:
        doc = self.docs[rel]
        for n in doc.all_nodes():
            if n.kind == kind and n.name.lower() == name.lower() and (parent is None or (n.parent and n.parent.name.lower() == parent.lower())):
                return n
        raise EditError(f"Lost track of {kind} {name!r} after an edit (internal error).")

    def _set(self, rel: str, doc: Doc) -> None:
        self.docs[rel] = doc

    # ------------------------------------------------------------------ properties
    def _apply_props(self, rel: str, kind: str, name: str, parent: str | None, props: dict[str, str | bool | None]) -> None:
        for key, val in props.items():
            node = self._refind(rel, kind, name, parent)
            doc = self.docs[rel]
            if key == "description":
                self._set(rel, _set_description(doc, node, None if val in (None, "") else str(val)))
            elif key in ("isHidden",):
                existing = node.props.get("isHidden")
                if val and existing is None:
                    at = max([p.end for p in node.props.values()] or [node.expr_end])
                    self._set(rel, tmdl.replace_span(doc, at, at, ["\t" * (node.indent + 1) + "isHidden"]))
                elif not val and existing is not None:
                    self._set(rel, tmdl.replace_span(doc, existing.start, existing.end, []))
            else:
                self._set(rel, tmdl.set_property(doc, node, key, None if val is None else str(val)))

    # ------------------------------------------------------------------ measures
    def upsert_measure(self, table: str, name: str, expression: str, *, format_string: str | None = None,
                       description: str | None = None, display_folder: str | None = None, hidden: bool | None = None) -> str:
        if not name.strip() or not expression.strip():
            raise EditError("name and expression are required.")
        if "\n" in name:
            raise EditError("name must be a single line.")
        problems = daxrefs.balanced(expression)
        if problems:
            raise EditError(f"Expression looks malformed: {', '.join(problems)}.")
        clash = self.model.find_measure(name)
        rel, tnode = self._table(table)
        if clash and clash[0].lower() != tnode.name.lower() and not self._deleted_measure(clash):
            raise EditError(f"A measure named {name!r} already exists in table {clash[0]!r}; measure names must be unique across the model. Use move_measure or pick another name.")
        if tnode.child_ci("column", name):
            raise EditError(f"Table {tnode.name!r} has a column named {name!r}; a measure cannot share its name.")
        existing = tnode.child_ci("measure", name)
        if existing is not None:
            name = existing.name                                  # keep the declared casing
        doc = self.docs[rel]
        if existing is not None:
            block = tmdl.render_expression_block("measure", existing.name, expression, existing.indent, [])
            self._set(rel, tmdl.replace_span(doc, existing.start, existing.expr_end, block))
            action = "updated"
        else:
            props = [("lineageTag", tmdl.new_lineage_tag())]
            block = tmdl.render_expression_block("measure", name, expression, tnode.indent + 1, props)
            self._set(rel, tmdl.insert_child(doc, tnode, block))
            action = "created"
        props2: dict[str, str | bool | None] = {}
        if format_string is not None:
            props2["formatString"] = format_string or None
        if display_folder is not None:
            props2["displayFolder"] = display_folder or None
        if hidden is not None:
            props2["isHidden"] = hidden
        if description is not None:
            props2["description"] = description
        self._apply_props(rel, "measure", name, tnode.name, props2)
        return f"{action} measure {tnode.name}[{name}]"

    def _deleted_measure(self, clash) -> bool:
        return False

    def upsert_calculated_column(self, table: str, name: str, expression: str, *, data_type: str | None = None,
                                 format_string: str | None = None, description: str | None = None, hidden: bool | None = None) -> str:
        problems = daxrefs.balanced(expression)
        if problems:
            raise EditError(f"Expression looks malformed: {', '.join(problems)}.")
        tmdl.check_single_line(name, "name")
        _enum(data_type, _DATA_TYPES, "data_type")
        rel, tnode = self._table(table)
        if tnode.child_ci("measure", name):
            raise EditError(f"{tnode.name!r} already has a measure named {name!r}.")
        doc = self.docs[rel]
        existing = tnode.child_ci("column", name)
        if existing is not None:
            name = existing.name
            if existing.expr is None:
                raise EditError(f"{tnode.name}[{name}] is a data column, not a calculated column; refusing to turn it into one.")
            block = tmdl.render_expression_block("column", existing.name, expression, existing.indent, [])
            self._set(rel, tmdl.replace_span(doc, existing.start, existing.expr_end, block))
            action = "updated"
        else:
            props = [("lineageTag", tmdl.new_lineage_tag())]
            if data_type:
                props.insert(0, ("dataType", data_type))
            block = tmdl.render_expression_block("column", name, expression, tnode.indent + 1, props)
            block.append("\t" * (tnode.indent + 2) + "summarizeBy: none")
            self._set(rel, tmdl.insert_child(doc, tnode, block))
            action = "created"
        props2: dict[str, str | bool | None] = {}
        if format_string is not None:
            props2["formatString"] = format_string or None
        if description is not None:
            props2["description"] = description
        if hidden is not None:
            props2["isHidden"] = hidden
        if data_type and existing is not None:
            props2["dataType"] = data_type
        self._apply_props(rel, "column", name, tnode.name, props2)
        return f"{action} calculated column {tnode.name}[{name}]"

    def set_properties(self, kind: str, table: str, name: str, props: dict[str, Any]) -> str:
        """Set simple properties (formatString, displayFolder, description, isHidden, dataCategory, summarizeBy, sortByColumn)."""
        allowed = _SIMPLE_PROPS | {"isHidden"}
        bad = set(props) - allowed
        if bad:
            raise EditError(f"Unsupported properties {sorted(bad)}; allowed: {sorted(allowed)}.")
        if kind not in ("measure", "column", "table"):
            raise EditError("kind must be measure, column or table.")
        _enum(props.get("summarizeBy"), _SUMMARIZE, "summarizeBy")
        _enum(props.get("dataType"), _DATA_TYPES, "dataType")
        rel, tnode = self._table(table)
        if kind == "table":
            self._apply_props(rel, "table", tnode.name, None, props)
        else:
            if tnode.child(kind, name) is None:
                raise EditError(f"{kind} {tnode.name}[{name}] not found.")
            self._apply_props(rel, kind, name, tnode.name, props)
        return f"set {sorted(props)} on {kind} {tnode.name}{'' if kind == 'table' else f'[{name}]'}"

    def delete_object(self, kind: str, table: str, name: str | None = None) -> str:
        rel, tnode = self._table(table)
        if kind == "table":
            self._delete_table(rel, tnode)
            return f"deleted table {tnode.name} (and its relationships, role filters and model reference)"
        if kind not in ("measure", "column", "hierarchy"):
            raise EditError("kind must be table, measure, column or hierarchy.")
        node = tnode.child(kind, name or "")
        if node is None:
            raise EditError(f"{kind} {tnode.name}[{name}] not found.")
        self._set(rel, tmdl.remove_node(self.docs[rel], node))
        return f"deleted {kind} {tnode.name}[{name}]"

    def _delete_table(self, rel: str, tnode: Node) -> None:
        name = tnode.name
        self.deleted.add(rel)
        self.docs.pop(rel, None)
        for r, d in list(self.docs.items()):
            changed = d
            for n in list(d.all_nodes()):
                if n.kind == "ref" and _ref_table_name(n) == name.lower():
                    cur = self._refind_ref(r, n.start)
                    changed = tmdl.replace_span(changed, cur.start, cur.end, []) if cur else changed
                    break
            self.docs[r] = changed
        for rr in self.model.relationships():
            if name.lower() in ((rr["from_table"] or "").lower(), (rr["to_table"] or "").lower()):
                self._drop_relationship(rr["id"])
        for role in self.model.roles():
            for tp in role.node.kids("tablePermission"):
                if tp.name.lower() == name.lower() and role.file in self.docs:
                    node = self._refind(role.file, "tablePermission", tp.name, role.node.name)
                    self._set(role.file, tmdl.remove_node(self.docs[role.file], node))

    def _refind_ref(self, rel: str, start: int) -> Node | None:
        for n in self.docs[rel].all_nodes():
            if n.kind == "ref" and n.start == start:
                return n
        return None

    # ------------------------------------------------------------------ relationships
    def _rel_file(self) -> str:
        for rel, d in self.docs.items():
            if any(n.kind == "relationship" for n in d.nodes):
                return rel
        return "relationships.tmdl"

    def add_relationship(self, from_table: str, from_column: str, to_table: str, to_column: str, *, cross_filter: str | None = None,
                         active: bool = True, from_cardinality: str | None = None, to_cardinality: str | None = None) -> str:
        for t, c in ((from_table, from_column), (to_table, to_column)):
            _, tn = self._table(t)
            if tn.child("column", c) is None:
                raise EditError(f"Column {t}[{c}] not found.")
        for r in self.model.relationships():
            if (r["from_table"], r["from_column"], r["to_table"], r["to_column"]) == (from_table, from_column, to_table, to_column):
                raise EditError("That relationship already exists.")
        if cross_filter and cross_filter not in ("oneDirection", "bothDirections", "automatic"):
            raise EditError("cross_filter must be oneDirection, bothDirections or automatic.")
        _enum(from_cardinality, _CARDINALITY, "from_cardinality")
        _enum(to_cardinality, _CARDINALITY, "to_cardinality")
        rid = tmdl.new_lineage_tag()
        lines = [f"relationship {rid}"]
        if from_cardinality:
            lines.append(f"\tfromCardinality: {from_cardinality}")
        if to_cardinality:
            lines.append(f"\ttoCardinality: {to_cardinality}")
        if not active:
            lines.append("\tisActive: false")
        if cross_filter and cross_filter != "oneDirection":
            lines.append(f"\tcrossFilteringBehavior: {cross_filter}")
        lines.append(f"\tfromColumn: {quote_name(from_table)}.{quote_name(from_column)}")
        lines.append(f"\ttoColumn: {quote_name(to_table)}.{quote_name(to_column)}")
        rel = self._rel_file()
        doc = self.docs.get(rel) or tmdl.parse("")
        tail = [""] if doc.lines and doc.lines[-1].strip() else []
        self._set(rel, tmdl.replace_span(doc, len(doc.lines), len(doc.lines), tail + lines))
        return f"added relationship {from_table}[{from_column}] -> {to_table}[{to_column}]"

    def _drop_relationship(self, rid: str) -> bool:
        for rel, d in self.docs.items():
            for n in d.nodes:
                if n.kind == "relationship" and n.name == rid:
                    self._set(rel, tmdl.remove_node(d, n))
                    return True
        return False

    def delete_relationship(self, from_table: str, from_column: str, to_table: str, to_column: str) -> str:
        for r in self.model.relationships():
            if (r["from_table"].lower(), r["from_column"].lower(), r["to_table"].lower(), r["to_column"].lower()) == \
                    (from_table.lower(), from_column.lower(), to_table.lower(), to_column.lower()):
                self._drop_relationship(r["id"])
                return f"deleted relationship {from_table}[{from_column}] -> {to_table}[{to_column}]"
        raise EditError("No such relationship.")

    def set_relationship(self, from_table: str, from_column: str, to_table: str, to_column: str, *, active: bool | None = None,
                         cross_filter: str | None = None) -> str:
        for r in self.model.relationships():
            if (r["from_table"].lower(), r["from_column"].lower(), r["to_table"].lower(), r["to_column"].lower()) == \
                    (from_table.lower(), from_column.lower(), to_table.lower(), to_column.lower()):
                for rel, d in self.docs.items():
                    for n in d.nodes:
                        if n.kind == "relationship" and n.name == r["id"]:
                            if active is not None:
                                self._set(rel, tmdl.set_property(self.docs[rel], self._refind(rel, "relationship", n.name), "isActive", None if active else "false"))
                            if cross_filter is not None:
                                if cross_filter not in ("oneDirection", "bothDirections", "automatic"):
                                    raise EditError("cross_filter must be oneDirection, bothDirections or automatic.")
                                self._set(rel, tmdl.set_property(self.docs[rel], self._refind(rel, "relationship", n.name), "crossFilteringBehavior",
                                                                 None if cross_filter == "oneDirection" else cross_filter))
                            return "updated relationship"
        raise EditError("No such relationship.")

    # ------------------------------------------------------------------ RLS
    def upsert_role(self, name: str, filters: dict[str, str], description: str | None = None) -> str:
        """Create a role, or set the filters of the listed tables on an existing one. Everything else in an existing role
        (other table filters, column permissions, annotations, model permission) is kept."""
        for t, f in filters.items():
            self._table(t)
            problems = daxrefs.balanced(f)
            if problems:
                raise EditError(f"Filter for {t!r} looks malformed: {', '.join(problems)}.")
        tmdl.check_single_line(name, "role name")
        found = None
        for r, d in self.docs.items():
            for n in d.nodes:
                if n.kind == "role" and n.name.lower() == name.lower():
                    found = (r, n)
        if found is None:
            rel = f"roles/{_safe_file(name)}.tmdl"
            if any(r.lower() == rel.lower() for r in self.docs) or (self.root / rel).exists():
                raise EditError(f"The file {rel} already exists (another role with a similar name); choose a different role name.")
            lines = [f"role {quote_name(name)}", "\tmodelPermission: read", ""]
            for t, f in filters.items():
                lines += tmdl.render_expression_block("tablePermission", t, f, 1, [])
            text = "\n".join(lines) + "\n"
            self.docs[rel] = tmdl.parse(text)
            self.created[rel] = text
            return f"created role {name}"
        rel, role = found
        for t, f in filters.items():
            role = self._refind(rel, "role", role.name)
            tp = next((c for c in role.kids("tablePermission") if c.name.lower() == t.lower()), None)
            block = tmdl.render_expression_block("tablePermission", tp.name if tp else t, f, role.indent + 1, [])
            doc = self.docs[rel]
            if tp is not None:
                self._set(rel, tmdl.replace_span(doc, tp.start, tp.expr_end, block))
            else:
                self._set(rel, tmdl.insert_child(doc, role, block, before_kinds=()))
        return f"updated role {name}: set the filter on {', '.join(filters)}; its other filters, permissions and annotations are unchanged"

    # ------------------------------------------------------------------ source M
    def set_partition_source(self, table: str, m_expression: str) -> str:
        rel, tnode = self._table(table)
        parts = tnode.kids("partition")
        if len(parts) != 1:
            raise EditError(f"{tnode.name!r} has {len(parts)} partitions; edit it in Desktop or pass a single-partition table.")
        p = parts[0]
        if (p.expr or "").strip() != "m":
            raise EditError(f"Partition {p.name!r} is not an M (Power Query) partition.")
        src = p.props.get("source")
        pad = "\t" * (p.indent + 1)
        body = "\n".join(("\t" * (p.indent + 3) + ln) if ln.strip() else "" for ln in m_expression.strip("\n").split("\n"))
        new = [f"{pad}source ="] + body.split("\n")
        doc = self.docs[rel]
        if src is None:
            at = max([q.end for q in p.props.values()] or [p.expr_end])
            self._set(rel, tmdl.replace_span(doc, at, at, new))
        else:
            self._set(rel, tmdl.replace_span(doc, src.start, src.end, new))
        return f"replaced Power Query source of {tnode.name}"

    def upsert_shared_expression(self, name: str, m_expression: str, description: str | None = None) -> str:
        rel = None
        existing = None
        for r, d in self.docs.items():
            for n in d.nodes:
                if n.kind == "expression" and n.name.lower() == name.lower():
                    existing, rel = n, r
        block = tmdl.render_expression_block("expression", name, m_expression, 0, [], doc=description)
        if existing is not None and rel:
            self._set(rel, tmdl.replace_span(self.docs[rel], existing.span_start, existing.expr_end, block))
            return f"updated shared query {name}"
        rel = "expressions.tmdl"
        doc = self.docs.get(rel) or tmdl.parse("")
        block = block + ["\tlineageTag: " + tmdl.new_lineage_tag()] if not any(l.strip().startswith("lineageTag") for l in block) else block
        tail = [""] if doc.lines and doc.lines[-1].strip() else []
        self._set(rel, tmdl.replace_span(doc, len(doc.lines), len(doc.lines), tail + block))
        return f"created shared query {name}"

    # ------------------------------------------------------------------ renames
    def rename(self, kind: str, table: str, old: str | None, new: str) -> str:
        if not new.strip():
            raise EditError("new name is required.")
        tmdl.check_single_line(new, "new name")
        if kind == "table":
            return self._rename_table(table, new)
        if kind not in ("measure", "column"):
            raise EditError("kind must be table, measure or column.")
        rel, tnode = self._table(table)
        node = tnode.child_ci(kind, old or "")
        if node is None:
            raise EditError(f"{kind} {tnode.name}[{old}] not found.")
        old = node.name
        if kind == "measure":
            hit = self.model.find_measure(new)
            same = hit is not None and hit[0].lower() == tnode.name.lower() and hit[1].node.name.lower() == old.lower()
            other_col = tnode.child_ci("column", new)
            if (hit and not same) or other_col:
                raise EditError(f"The name {new!r} is already used in the model.")
        else:
            c, m2 = tnode.child_ci("column", new), tnode.child_ci("measure", new)
            if (c is not None and c is not node) or m2 is not None:
                raise EditError(f"{tnode.name!r} already has a field named {new!r}.")
        # 1) declaration
        doc = self.docs[rel]
        head = node.decl_line
        m = re.match(r"^(\s*\w+\s+)", head)
        rest = head[m.end():]
        _, remainder = tmdl._read_name(rest)
        self._set(rel, tmdl.replace_span(doc, node.start, node.start + 1, [f"{m.group(1)}{quote_name(new)}{remainder}"]))
        # 2) DAX everywhere
        col_index = self._col_index()
        count = self._rewrite_all_dax(lambda text, owner: daxrefs.rename_in_expr(
            text, kind=kind, table=tnode.name, old=old, new=new, owner_table=owner, col_index=col_index))
        # 3) structural references to a renamed column
        if kind == "column":
            self._rename_column_structural(tnode.name, old, new)
        # 4) report
        rep = self._rename_report(kind, tnode.name, old, new)
        note = "; bare [name] references in other tables' expressions are not rewritten, so check the validation result" if kind == "column" else ""
        return f"renamed {kind} {tnode.name}[{old}] -> {new}; rewrote {count} DAX reference(s), {len(rep)} report file(s){note}"

    def _col_index(self) -> dict[str, set[str]]:
        return {n.name.lower(): {c.name.lower() for c in n.kids("column")}
                for d in self.docs.values() for n in d.nodes if n.kind == "table"}

    def _rename_column_structural(self, table: str, old: str, new: str) -> None:
        for rel, d in list(self.docs.items()):
            for n in list(d.all_nodes()):
                if n.kind == "relationship":
                    for side in ("fromColumn", "toColumn"):
                        val = n.prop(side) or ""
                        parts = tmdl.split_dotted(val)
                        if len(parts) == 2 and parts[0].lower() == table.lower() and parts[1].lower() == old.lower():
                            cur = self._refind(rel, "relationship", n.name)
                            self._set(rel, tmdl.set_property(self.docs[rel], cur, side, f"{quote_name(parts[0])}.{quote_name(new)}"))
                elif n.kind == "column" and n.parent and n.parent.name.lower() == table.lower():
                    sbc = n.prop("sortByColumn")
                    if sbc and tmdl.unquote_name(sbc).lower() == old.lower():
                        cur = self._refind(rel, "column", n.name, table)
                        self._set(rel, tmdl.set_property(self.docs[rel], cur, "sortByColumn", quote_name(new)))
                elif n.kind == "level" and n.prop("column") and tmdl.unquote_name(n.prop("column")).lower() == old.lower() \
                        and n.parent and n.parent.parent and n.parent.parent.name.lower() == table.lower():
                    cur = self._refind(rel, "level", n.name, n.parent.name)
                    self._set(rel, tmdl.set_property(self.docs[rel], cur, "column", quote_name(new)))

    def _rename_table(self, old: str, new: str) -> str:
        rel, tnode = self._table(old)
        if any(t.node.name.lower() == new.lower() for t in self._live_tables()):
            raise EditError(f"A table named {new!r} already exists.")
        oldname = tnode.name
        doc = self.docs[rel]
        head = tnode.decl_line
        m = re.match(r"^(\s*table\s+)", head)
        _, remainder = tmdl._read_name(head[m.end():])
        doc = tmdl.replace_span(doc, tnode.start, tnode.start + 1, [f"{m.group(1)}{quote_name(new)}{remainder}"])
        tmdl.check_single_line(new, "new table name")
        new_rel = f"tables/{_safe_file(new)}.tmdl" if rel.startswith("tables/") else rel
        if new_rel != rel and (new_rel in self.docs or (self.root / new_rel).exists()):
            raise EditError(f"A file for that name already exists ({new_rel}); choose a different table name.")
        if new_rel != rel:
            self.docs.pop(rel)
            self.deleted.add(rel)
            self.docs[new_rel] = doc
            self.created[new_rel] = ""  # marker: new path
        else:
            self.docs[rel] = doc
        for r, d in list(self.docs.items()):
            for n in d.all_nodes():
                if n.kind == "ref" and _ref_table_name(n) == oldname.lower():
                    cur = self._refind_ref(r, n.start)
                    pad = "\t" * cur.indent
                    self._set(r, tmdl.replace_span(self.docs[r], cur.start, cur.end, [pad + "ref table " + quote_name(new)]))
                    break
        for r, d in list(self.docs.items()):
            for n in d.all_nodes():
                if n.kind == "relationship":
                    for side in ("fromColumn", "toColumn"):
                        parts = tmdl.split_dotted(n.prop(side) or "")
                        if len(parts) == 2 and parts[0].lower() == oldname.lower():
                            cur = self._refind(r, "relationship", n.name)
                            self._set(r, tmdl.set_property(self.docs[r], cur, side, f"{quote_name(new)}.{quote_name(parts[1])}"))
                elif n.kind == "tablePermission" and n.name.lower() == oldname.lower():
                    cur = self._refind(r, "tablePermission", n.name, n.parent.name if n.parent else None)
                    head = cur.decl_line
                    mm = re.match(r"^(\s*tablePermission\s+)", head)
                    _, rem = tmdl._read_name(head[mm.end():])
                    self._set(r, tmdl.replace_span(self.docs[r], cur.start, cur.start + 1, [f"{mm.group(1)}{quote_name(new)}{rem}"]))
        count = self._rewrite_all_dax(lambda text, owner: daxrefs.rename_in_expr(text, kind="table", table=None, old=oldname, new=new, owner_table=owner))
        rep = self._rename_report("table", None, oldname, new)
        return f"renamed table {oldname} -> {new}; rewrote {count} DAX reference(s), {len(rep)} report file(s)"

    def _rename_report(self, kind: str, table: str | None, old: str, new: str) -> dict[str, str | None]:
        if self.project.pbir_dir is None:
            return {}
        rep = pbir.Report.load(self.project.pbir_dir)
        edits = pbir.rename_field_refs(rep, kind, table, old, new)
        base = self.project.pbir_dir
        for rel, text in edits.items():
            self.report_edits[str((base / rel).resolve())] = text
        return edits

    def _rewrite_all_dax(self, fn: Callable[[str, str | None], tuple[str, int]]) -> int:
        """Apply `fn(code, owner_table) -> (new_code, n)` to every DAX body in every doc."""
        total = 0
        for rel in list(self.docs):
            doc = self.docs[rel]
            spans = _code_spans(doc)
            lines = list(doc.lines)
            for (start, end, first_col, owner) in sorted(spans, reverse=True):
                head = lines[start]
                prefix, code_first = head[:first_col], head[first_col:]
                code = "\n".join([code_first] + lines[start + 1:end])
                new_code, n = fn(code, owner)
                if n:
                    total += n
                    new_lines = new_code.split("\n")
                    lines[start:end] = [prefix + new_lines[0]] + new_lines[1:]
            if lines != doc.lines:
                self.docs[rel] = tmdl.parse(("﻿" if doc.bom else "") + doc.eol.join(lines) + (doc.eol if doc.trailing_newline else ""))
        return total

    def move_measure(self, from_table: str, name: str, to_table: str) -> str:
        rel, tnode = self._table(from_table)
        node = tnode.child("measure", name)
        if node is None:
            raise EditError(f"Measure {from_table}[{name}] not found.")
        rel2, t2 = self._table(to_table)
        text_lines = self.docs[rel].lines[node.span_start:node.end]
        shift = (t2.indent + 1) - node.indent
        moved = [(ln if not ln.strip() else (("\t" * shift + ln) if shift > 0 else ln[-shift:])) for ln in text_lines]
        self._set(rel, tmdl.remove_node(self.docs[rel], node))
        t2 = self._refind(rel2, "table", t2.name)
        self._set(rel2, tmdl.insert_child(self.docs[rel2], t2, moved))
        # unqualified/qualified references to the measure keep working only if the table prefix is not used
        count = self._rewrite_all_dax(lambda text, owner: _requalify(text, name, from_table, to_table))
        rep = self._rename_measure_entity(name, from_table, to_table)
        return f"moved measure {name} from {from_table} to {to_table}; rewrote {count} qualified reference(s), {len(rep)} report file(s)"

    def _rename_measure_entity(self, name: str, from_table: str, to_table: str) -> dict[str, str | None]:
        if self.project.pbir_dir is None:
            return {}
        rep = pbir.Report.load(self.project.pbir_dir)
        out: dict[str, str | None] = {}
        import json as _json
        for rel, data in rep.files.items():
            copy = _json.loads(_json.dumps(data))
            n = _retarget(copy, name, from_table, to_table, {})
            if n:
                out[rel] = pbir.dumps(copy)
        for rel, text in out.items():
            self.report_edits[str((self.project.pbir_dir / rel).resolve())] = text
        return out

    # ------------------------------------------------------------------ report edits
    def merge_report_edits(self, edits: dict[str, str | None]) -> None:
        base = self.project.pbir_dir
        if base is None:
            raise EditError("This project has no PBIR report definition (report folder missing or legacy report.json).")
        for rel, text in edits.items():
            self.report_edits[str((base / rel).resolve())] = text

    # ------------------------------------------------------------------ result
    def edits(self) -> dict[Path, str | None]:
        out: dict[Path, str | None] = {}
        for rel, doc in self.docs.items():
            text = doc.to_text()
            if self.original.get(rel) != text:
                out[(self.root / rel).resolve()] = text
        for rel in self.deleted:
            if rel not in self.docs:
                out[(self.root / rel).resolve()] = None
        for p, text in self.report_edits.items():
            out[Path(p)] = text
        return out


def _ref_table_name(n: Node) -> str | None:
    """Lower-cased table name of a `ref table X` line, else None."""
    m = re.match(r"^table\s+(.+)$", n.name.strip())
    return tmdl.unquote_name(m.group(1)).lower() if m else None


def _owner_table(n: Node) -> str | None:
    cur: Node | None = n
    while cur is not None:
        if cur.kind == "table":
            return cur.name
        cur = cur.parent
    return None


def _code_spans(doc: Doc) -> list[tuple[int, int, int, str | None]]:
    """(start line, end line exclusive, column where code begins on first line, owner table)."""
    spans: list[tuple[int, int, int, str | None]] = []
    for n in doc.all_nodes():
        owner = _owner_table(n)
        if n.kind in ("measure", "column", "function", "calculationItem", "table", "tablePermission",
                      "formatStringDefinition", "detailRowsDefinition") and n.expr is not None:
            if n.fenced:
                spans.append((n.start + 1, n.expr_end - 1, 0, owner))          # lines between the ``` fences
            else:
                col = _expr_col(n.decl_line)
                if col is not None:
                    spans.append((n.start, n.expr_end, col, n.name if n.kind in ("table", "tablePermission") else owner))
        elif n.kind == "partition" and (n.expr or "").strip() == "calculated" and "source" in n.props:
            p = n.props["source"]
            col = _prop_expr_col(doc.lines[p.start])
            if col is not None:
                spans.append((p.start, p.end, col, owner))
    return spans


def _expr_col(decl: str) -> int | None:
    m = re.match(r"^(\s*\w+\s+)", decl)
    if not m:
        return None
    _, remainder = tmdl._read_name(decl[m.end():])
    if not remainder.strip().startswith("="):
        return None
    eq = len(decl) - len(remainder) + remainder.index("=") + 1
    return eq


def _prop_expr_col(line: str) -> int | None:
    i = line.find("=")
    return None if i < 0 else i + 1


def _requalify(text: str, name: str, from_table: str, to_table: str) -> tuple[str, int]:
    count = 0
    for r in sorted(daxrefs.refs(text), key=lambda r: r.span[0], reverse=True):
        if r.name.lower() == name.lower() and r.table and r.table.lower() == from_table.lower():
            text = text[:r.span[0]] + f"{quote_name(to_table)}[{name}]" + text[r.span[1]:]
            count += 1
    return text, count


def _retarget(node: Any, name: str, from_table: str, to_table: str, aliases: dict[str, str]) -> int:
    count = 0
    if isinstance(node, dict):
        aliases = dict(aliases)
        if isinstance(node.get("From"), list):
            for f in node["From"]:
                if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                    aliases[f["Name"]] = f["Entity"]
        inner = node.get("Measure")
        if isinstance(inner, dict) and inner.get("Property") == name:
            sr = (inner.get("Expression") or {}).get("SourceRef") or {}
            ent = sr.get("Entity") or aliases.get(sr.get("Source", ""))
            if ent and ent.lower() == from_table.lower():
                if "Entity" in sr:
                    sr["Entity"] = to_table
                else:
                    sr.pop("Source", None)
                    sr["Entity"] = to_table
                count += 1
        for k in ("queryRef",):
            v = node.get(k)
            if isinstance(v, str) and v.lower() == f"{from_table}.{name}".lower():
                node[k] = f"{to_table}.{name}"
        for v in node.values():
            count += _retarget(v, name, from_table, to_table, aliases)
    elif isinstance(node, list):
        for v in node:
            count += _retarget(v, name, from_table, to_table, aliases)
    return count


def _set_description(doc: Doc, node: Node, text: str | None) -> Doc:
    pad = "\t" * node.indent
    new = [] if text is None else [f"{pad}/// {ln}" if ln else f"{pad}///" for ln in text.splitlines()]
    if node.doc_start is not None:
        return tmdl.replace_span(doc, node.doc_start, node.start, new)
    return tmdl.replace_span(doc, node.start, node.start, new)
