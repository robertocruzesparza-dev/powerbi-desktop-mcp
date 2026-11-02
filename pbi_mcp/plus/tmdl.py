"""Lossless TMDL parser with line-span surgery.

TMDL (Tabular Model Definition Language) is the text format Power BI Desktop uses for the
semantic-model half of a .pbip project. This module parses it into a tree of `Node`s that
remember the exact line span they came from, so an edit rewrites only the lines it owns and
every other byte of the file is preserved (Desktop's own formatting, lineage tags, comments).

Grammar subset handled (verified against Microsoft's documented examples and fixtures):
  * object declaration:   <keyword> <name> [= <expression>]      (tab indented, children +1)
  * property:             <name>: <value>   |   <name> = <expression>   |   <flag>
  * multi-line expression: continuation lines indented >= declaration indent + 2, or a
    ``` fenced block
  * names quoted with single quotes ('' escapes a quote)
  * /// doc-comment lines attach to the next object as its description
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Iterator

# Words that open a child object. Anything else on a line is a property.
KEYWORDS = frozenset({
    "database", "model", "table", "column", "measure", "hierarchy", "level", "partition",
    "relationship", "role", "tablePermission", "columnPermission", "perspective",
    "perspectiveTable", "perspectiveColumn", "perspectiveMeasure", "perspectiveHierarchy",
    "culture", "expression", "dataSource", "calculationGroup", "calculationItem", "function",
    "annotation", "extendedProperty", "linguisticMetadata", "ref", "queryGroup", "calendar",
    "calendarColumnGroup", "variation", "refreshPolicy", "detailRowsDefinition", "formatStringDefinition",
    "kpi", "alternateOf", "translation", "objectTranslation", "member", "changedProperty",
    "createOrReplace", "dax",
})
# Keywords whose `= ...` is a real (possibly multi-line) expression
_EXPR_KINDS = frozenset({
    "measure", "column", "table", "expression", "calculationItem", "function", "dax",
    "formatStringDefinition", "detailRowsDefinition",
})
_PLAIN_NAME = re.compile(r"^[^\W\d][\w]*$", re.UNICODE)


class TmdlError(ValueError):
    """Raised when TMDL text cannot be understood well enough to edit safely."""


def check_single_line(value: str, what: str = "value") -> str:
    """TMDL is line based: a line break inside a name or scalar property would inject new objects."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{what} must be a single line with no control characters.")
    return value


def quote_name(name: str) -> str:
    """Render an object name the way TMDL expects (single-quoted unless a plain identifier)."""
    check_single_line(name, "name")
    if _PLAIN_NAME.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def unquote_name(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def split_dotted(ref: str) -> list[str]:
    """Split 'Table'.'Col' / Table.Col into parts, honouring single quotes."""
    parts: list[str] = []
    i, n = 0, len(ref)
    while i < n:
        if ref[i] == "'":
            j = i + 1
            buf = []
            while j < n:
                if ref[j] == "'":
                    if j + 1 < n and ref[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(ref[j])
                j += 1
            parts.append("".join(buf))
            i = j + 1
            if i < n and ref[i] == ".":
                i += 1
        else:
            j = ref.find(".", i)
            if j == -1:
                parts.append(ref[i:].strip())
                break
            parts.append(ref[i:j].strip())
            i = j + 1
    return parts


@dataclass
class Prop:
    name: str
    value: str                 # text after ':' or the expression text for '= ...' props
    start: int                 # first line index
    end: int                   # exclusive
    is_expr: bool = False
    flag: bool = False         # bare keyword property such as `isHidden`


@dataclass
class Node:
    kind: str
    name: str
    indent: int
    start: int
    end: int                   # exclusive; trailing blank lines excluded
    decl_line: str
    expr: str | None = None
    expr_end: int = 0          # exclusive end of declaration + expression lines
    fenced: bool = False
    props: dict[str, Prop] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    doc: list[str] = field(default_factory=list)
    doc_start: int | None = None
    parent: "Node | None" = None

    def prop(self, name: str, default: str | None = None) -> str | None:
        p = self.props.get(name)
        if p is None:
            return default
        return "true" if p.flag and not p.value else p.value

    def flag(self, name: str) -> bool:
        p = self.props.get(name)
        if p is None:
            return False
        if p.flag and not p.value:
            return True
        return p.value.strip().lower() == "true"

    def child_ci(self, kind: str, name: str) -> "Node | None":
        """Like child(), but case-insensitive (Power BI object names are)."""
        low = name.lower()
        for c in self.children:
            if c.kind == kind and c.name.lower() == low:
                return c
        return None

    def kids(self, kind: str) -> list["Node"]:
        return [c for c in self.children if c.kind == kind]

    def child(self, kind: str, name: str) -> "Node | None":
        for c in self.children:
            if c.kind == kind and c.name == name:
                return c
        return None

    @property
    def description(self) -> str | None:
        if self.doc:
            return "\n".join(self.doc)
        return self.prop("description")

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def span_start(self) -> int:
        return self.doc_start if self.doc_start is not None else self.start


def _indent_of(raw: str) -> int:
    tabs = 0
    i = 0
    while i < len(raw) and raw[i] == "\t":
        tabs += 1
        i += 1
    spaces = 0
    while i < len(raw) and raw[i] == " ":
        spaces += 1
        i += 1
    return tabs + spaces // 4


def _read_name(rest: str) -> tuple[str, str]:
    """Return (name, remainder-after-name) from the text following a keyword."""
    rest = rest.lstrip()
    if rest.startswith("'"):
        i, n, buf = 1, len(rest), []
        while i < n:
            if rest[i] == "'":
                if i + 1 < n and rest[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                return "".join(buf), rest[i + 1:]
            buf.append(rest[i])
            i += 1
        raise TmdlError(f"Unterminated quoted name: {rest[:40]!r}")
    m = re.match(r"^([^\s=]+)(.*)$", rest, re.S)
    if not m:
        return "", rest
    return m.group(1), m.group(2)


_PROP_COLON = re.compile(r"^([A-Za-z_][\w.]*)\s*:\s?(.*)$")
_PROP_EQ = re.compile(r"^([A-Za-z_][\w.]*)\s*=\s?(.*)$")
_KEYWORD = re.compile(r"^([A-Za-z_]\w*)(?:\s+(.*))?$", re.S)


@dataclass
class Doc:
    """A parsed TMDL file. `lines` is the source split without line endings."""
    lines: list[str]
    nodes: list[Node]
    eol: str = "\n"
    trailing_newline: bool = True
    bom: bool = False

    def to_text(self) -> str:
        text = self.eol.join(self.lines)
        if self.trailing_newline:
            text += self.eol
        return ("﻿" if self.bom else "") + text

    def all_nodes(self) -> Iterator[Node]:
        for n in self.nodes:
            yield from n.walk()

    def find(self, kind: str, name: str) -> Node | None:
        for n in self.all_nodes():
            if n.kind == kind and n.name == name:
                return n
        return None

    def expression_of(self, node: Node) -> str:
        return node.expr or ""


def _continuation_lines(lines: list[str], i: int, owner_indent: int) -> tuple[int, bool]:
    """Index after the expression continuation of a line at `owner_indent` starting at lines[i]."""
    n = len(lines)
    j = i
    last = i
    while j < n:
        raw = lines[j]
        if not raw.strip():
            j += 1
            continue
        if _indent_of(raw) >= owner_indent + 2:
            j += 1
            last = j
            continue
        break
    return last, False


def _parse_block(lines: list[str], i: int, base: int, parent: Node | None) -> tuple[list[Node], dict[str, Prop], int]:
    nodes: list[Node] = []
    props: dict[str, Prop] = {}
    n = len(lines)
    pending_doc: list[str] = []
    doc_start: int | None = None
    while i < n:
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        ind = _indent_of(raw)
        if ind < base:
            break
        text = raw.strip()
        if ind > base:
            # stray deeper line (malformed or continuation we did not claim): skip, never guess
            i += 1
            continue
        if text.startswith("///"):
            if doc_start is None:
                doc_start = i
            pending_doc.append(text[3:].lstrip(" ") if text[3:4] == " " else text[3:])
            i += 1
            continue
        kw = _KEYWORD.match(text)
        word = kw.group(1) if kw else ""
        is_object = bool(kw) and word in KEYWORDS and not text.startswith(word + ":")
        if is_object and word in ("calendarColumnGroup",) and False:
            is_object = True
        if is_object:
            rest = kw.group(2) or ""
            if word == "ref":
                name = rest.strip()
                node = Node(word, name, ind, i, i + 1, raw, doc=pending_doc, doc_start=doc_start, parent=parent)
                node.expr_end = i + 1
                nodes.append(node)
                pending_doc, doc_start = [], None
                i += 1
                continue
            name, remainder = _read_name(rest) if rest else ("", "")
            node = Node(word, name, ind, i, i + 1, raw, doc=pending_doc, doc_start=doc_start, parent=parent)
            pending_doc, doc_start = [], None
            remainder = remainder.strip()
            j = i + 1
            if remainder.startswith("="):
                expr_first = remainder[1:].strip()
                if expr_first.startswith("```"):
                    node.fenced = True
                    body: list[str] = []
                    after = expr_first[3:]
                    if after.strip():
                        body.append(after)
                    while j < n and lines[j].strip() != "```":
                        body.append(lines[j])
                        j += 1
                    if j >= n:
                        raise TmdlError(f"Unterminated ``` block for {word} {name!r} at line {i + 1}")
                    j += 1
                    node.expr = _dedent(body)
                else:
                    cont_end, _ = _continuation_lines(lines, j, ind)
                    cont = lines[j:cont_end]
                    if expr_first and cont:
                        node.expr = expr_first + "\n" + _dedent(cont)
                    elif expr_first:
                        node.expr = expr_first
                    else:
                        node.expr = _dedent(cont)
                    j = cont_end
            elif remainder:
                node.expr = None
            node.expr_end = j
            sub_nodes, sub_props, end = _parse_block(lines, j, ind + 1, node)
            node.children = sub_nodes
            node.props = sub_props
            last = max(j, end)
            while last > node.start + 1 and not lines[last - 1].strip():
                last -= 1
            node.end = last
            nodes.append(node)
            i = last if last > i else i + 1
            continue
        # property
        pdoc, doc_start, pending_doc = pending_doc, None, []
        mc = _PROP_COLON.match(text)
        me = _PROP_EQ.match(text)
        if mc and (not me or text.index(":") < text.index("=")):
            props[mc.group(1)] = Prop(mc.group(1), mc.group(2).strip(), i, i + 1)
            i += 1
        elif me:
            first = me.group(2).strip()
            j = i + 1
            if first.startswith("```"):
                body = []
                if first[3:].strip():
                    body.append(first[3:])
                while j < n and lines[j].strip() != "```":
                    body.append(lines[j])
                    j += 1
                j += 1
                value = _dedent(body)
            else:
                cont_end, _ = _continuation_lines(lines, j, ind)
                cont = lines[j:cont_end]
                value = (first + "\n" + _dedent(cont)) if first and cont else (first or _dedent(cont))
                j = cont_end
            props[me.group(1)] = Prop(me.group(1), value, i, j, is_expr=True)
            i = j
        else:
            props[text] = Prop(text, "", i, i + 1, flag=True)
            i += 1
        _ = pdoc
    end = i
    return nodes, props, end


def _dedent(block: list[str]) -> str:
    """Remove the common leading tab indent of continuation lines, keep relative indentation."""
    real = [b for b in block if b.strip()]
    if not real:
        return ""
    common = min(len(b) - len(b.lstrip("\t")) for b in real)
    out = [b[common:] if b.strip() else "" for b in block]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def parse(text: str) -> Doc:
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    eol = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith("\n")
    lines = text.replace("\r\n", "\n").split("\n")
    if trailing:
        lines.pop()
    nodes, _props, _end = _parse_block(lines, 0, 0, None)
    return Doc(lines=lines, nodes=nodes, eol=eol, trailing_newline=trailing, bom=bom)


# --------------------------------------------------------------------------------------
# Rendering and surgery
# --------------------------------------------------------------------------------------

def new_lineage_tag() -> str:
    return str(uuid.uuid4())


def render_expression_block(keyword: str, name: str, expr: str, indent: int, props: list[tuple[str, str]],
                            flags: list[str] | None = None, doc: str | None = None) -> list[str]:
    """Render `keyword name = expr` plus property lines at the right tab depth."""
    pad = "\t" * indent
    out: list[str] = []
    if doc:
        out += [f"{pad}/// {d}" if d else f"{pad}///" for d in doc.splitlines()]
    expr = expr.strip("\n")
    if "\n" in expr:
        out.append(f"{pad}{keyword} {quote_name(name)} =")
        body_pad = "\t" * (indent + 2)
        out += [(body_pad + ln) if ln.strip() else "" for ln in expr.split("\n")]
    else:
        out.append(f"{pad}{keyword} {quote_name(name)} = {expr}")
    ppad = "\t" * (indent + 1)
    for key, value in props:
        out.append(f"{ppad}{key}: {check_single_line(value, key)}")
    for f in flags or []:
        out.append(f"{ppad}{f}")
    return out


def replace_span(doc: Doc, start: int, end: int, new_lines: list[str]) -> Doc:
    """Return a new Doc with lines[start:end] replaced (the old Doc is left untouched)."""
    lines = doc.lines[:start] + new_lines + doc.lines[end:]
    rebuilt = parse(("﻿" if doc.bom else "") + doc.eol.join(lines) + (doc.eol if doc.trailing_newline else ""))
    return rebuilt


def set_property(doc: Doc, node: Node, name: str, value: str | None) -> Doc:
    """Set / add / remove a simple `name: value` property on `node` (value None removes it)."""
    pad = "\t" * (node.indent + 1)
    check_single_line(name, "property name")
    if value is not None:
        check_single_line(value, f"property {name}")
    existing = node.props.get(name)
    if existing is not None:
        if value is None:
            return replace_span(doc, existing.start, existing.end, [])
        return replace_span(doc, existing.start, existing.end, [f"{pad}{name}: {value}"])
    if value is None:
        return doc
    at = node.expr_end
    if node.props:
        at = max(p.end for p in node.props.values())
    return replace_span(doc, at, at, [f"{pad}{name}: {value}"])


def remove_node(doc: Doc, node: Node) -> Doc:
    start, end = node.span_start, node.end
    # also eat one blank separator line after, if present, to avoid growing blank gaps
    if end < len(doc.lines) and not doc.lines[end].strip():
        end += 1
    return replace_span(doc, start, end, [])


def insert_child(doc: Doc, parent: Node, new_lines: list[str], before_kinds: tuple[str, ...] = ("partition",)) -> Doc:
    """Insert rendered lines as a child of `parent`, ahead of the first child of `before_kinds`."""
    at = parent.end
    for c in parent.children:
        if c.kind in before_kinds:
            at = c.span_start
            break
    sep = [""] if at > parent.start and doc.lines[at - 1].strip() else []
    tail = [""] if at < len(doc.lines) and at != parent.end and doc.lines[at].strip() else []
    return replace_span(doc, at, at, sep + new_lines + tail)


def format_summary(doc: Doc) -> dict:
    kinds: dict[str, int] = {}
    for n in doc.all_nodes():
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    return {"lines": len(doc.lines), "objects": kinds, "uses_spaces_indent": any(l.startswith(" ") for l in doc.lines if l.strip())}


def check(doc: Doc) -> list[str]:
    """Structural problems that make Desktop reject a TMDL file."""
    problems: list[str] = []
    code_lines: set[int] = set()  # lines inside DAX/M bodies may legitimately use spaces
    for n in doc.all_nodes():
        code_lines.update(range(n.start + 1, n.expr_end))
        for p in n.props.values():
            if p.is_expr:
                code_lines.update(range(p.start + 1, p.end))
    for i, line in enumerate(doc.lines, start=1):
        if not line.strip() or (i - 1) in code_lines:
            continue
        lead = line[: len(line) - len(line.lstrip())]
        if " " in lead and "\t" in lead:
            problems.append(f"line {i}: mixes tabs and spaces in the indent")
        elif " " in lead:
            problems.append(f"line {i}: indented with spaces (TMDL requires tabs)")
    seen: dict[tuple[str, str, str], int] = {}
    for n in doc.all_nodes():
        if n.kind in ("annotation", "extendedProperty", "ref", "changedProperty"):
            continue
        key = (n.parent.name if n.parent else "", n.kind, n.name.lower())
        if key in seen:
            problems.append(f"line {n.start + 1}: duplicate {n.kind} {n.name!r} (first at line {seen[key] + 1})")
        else:
            seen[key] = n.start
    return problems
