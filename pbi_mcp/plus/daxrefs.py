"""DAX reference extraction and light static linting (no engine required)."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Ref:
    table: str | None
    name: str
    span: tuple[int, int]      # span of the whole reference token in the *original* text


_STR = re.compile(r'"(?:[^"]|"")*"')
_LINE_COMMENT = re.compile(r"(//|--)[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_REF = re.compile(
    r"(?:(?P<qt>'(?:[^']|'')+')|(?P<bt>[A-Za-z_À-￿][\wÀ-￿ .]*?))?\s*\[(?P<name>(?:[^\]]|\]\])+)\]"
)
_BARE_TABLE = re.compile(r"([A-Za-z_À-￿][\wÀ-￿]*)$")


def _scan(expr: str) -> tuple[str, bool, bool]:
    """One left-to-right pass: (masked text, string left open, block comment left open).
    Strings and comments are blanked (newlines kept, same length); text inside [..] and '..' names is skipped so a quote
    or // inside a column name is never mistaken for code."""
    out = list(expr)
    n = len(expr)
    i = 0
    open_string = open_block = False

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = expr[i]
        if c == '"':
            j = i + 1
            closed = False
            while j < n:
                if expr[j] == '"':
                    if j + 1 < n and expr[j + 1] == '"':
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            blank(i, j + 1)
            open_string = open_string or not closed
            i = j + 1
        elif expr.startswith("//", i) or expr.startswith("--", i):
            j = expr.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif expr.startswith("/*", i):
            j = expr.find("*/", i + 2)
            if j < 0:
                open_block = True
                j = n
            else:
                j += 2
            blank(i, j)
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if expr[j] == "'":
                    if j + 1 < n and expr[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif c == "[":
            j = i + 1
            while j < n:
                if expr[j] == "]":
                    if j + 1 < n and expr[j + 1] == "]":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out), open_string, open_block


def mask(expr: str) -> str:
    """Blank out string literals and comments (same length) so regexes ignore them."""
    return _scan(expr)[0]


def _blank_brackets(masked: str) -> str:
    """Replace the inside of [..] names with spaces (brackets kept)."""
    return re.sub(r"\[(?:[^\]]|\]\])*\]", lambda m: "[" + " " * (len(m.group(0)) - 2) + "]", masked)


def refs(expr: str) -> list[Ref]:
    """Every Table[Col], 'My Table'[Col] and bare [Name] in `expr` (literals/comments ignored)."""
    masked = mask(expr)
    found: list[Ref] = []
    for m in re.finditer(r"\[((?:[^\]]|\]\])+)\]", masked):
        start = m.start()
        name = expr[m.start(1):m.end(1)].replace("]]", "]")
        table: str | None = None
        tstart = start
        before = masked[:start]
        if before.endswith("'"):
            i = before.rfind("'", 0, len(before) - 1)
            while i > 0 and before[i - 1] == "'":      # '' escape inside quoted table name
                i = before.rfind("'", 0, i - 1)
            if i >= 0:
                table = expr[i + 1:start - 1].replace("''", "'")
                tstart = i
        else:
            bm = _BARE_TABLE.search(before)
            if bm:
                table = bm.group(1)
                tstart = bm.start(1)
        found.append(Ref(table, name, (tstart, m.end())))
    return found


def rename_in_expr(expr: str, *, kind: str, table: str | None, old: str, new: str, owner_table: str | None,
                   col_index: dict[str, set[str]] | None = None) -> tuple[str, int]:
    """Rewrite references to a renamed measure/column inside one DAX expression.
    kind: 'measure' | 'column' | 'table'. Returns (new_expr, replacements)."""
    from .tmdl import quote_name
    masked = mask(expr)
    edits: list[tuple[int, int, str]] = []
    if kind == "table":
        flat = _blank_brackets(masked)
        pat = re.compile(r"'(?:[^']|'')+'|(?<![\w.\]\[])[A-Za-z_\u00C0-\uFFFF][\w\u00C0-\uFFFF]*")
        for m in pat.finditer(flat):
            tok = expr[m.start():m.end()]
            if tok.startswith("'"):
                nm = tok[1:-1].replace("''", "'")
            else:
                nm = tok
                if flat[m.end():].lstrip().startswith("("):
                    continue                      # a function call, not a table
            if nm.lower() == old.lower():
                edits.append((m.start(), m.end(), quote_name(new)))
    else:
        for r in refs(expr):
            if r.name.lower() != old.lower():
                continue
            if kind == "measure":
                if r.table is None:
                    # a bare [Old] is the measure unless the expression's own table has a column called Old
                    own_cols = (col_index or {}).get((owner_table or "").lower(), set())
                    hit = old.lower() not in own_cols
                else:
                    hit = table is not None and r.table.lower() == table.lower()
            else:  # column
                if r.table is None:
                    hit = owner_table is not None and table is not None and owner_table.lower() == table.lower()
                else:
                    hit = table is not None and r.table.lower() == table.lower()
            if hit:
                new_name = new.replace("]", "]]")
                prefix = quote_name(r.table) if r.table else ""
                edits.append((r.span[0], r.span[1], f"{prefix}[{new_name}]"))
    for s, e, text in sorted(edits, reverse=True):
        expr = expr[:s] + text + expr[e:]
    return expr, len(edits)


# --------------------------------------------------------------------------------------
# Static DAX lint. Each rule is a leads-not-proof heuristic with a stable id.
# --------------------------------------------------------------------------------------

@dataclass
class Lint:
    rule: str
    severity: str     # high | medium | info
    message: str


_RULES: list[tuple[str, str, re.Pattern, str]] = [
    ("DAX001", "medium", re.compile(r"\bIFERROR\s*\(", re.I),
     "IFERROR hides real errors and blocks engine optimisations; use DIVIDE or explicit checks."),
    ("DAX002", "medium", re.compile(r"\bFILTER\s*\(\s*ALL\s*\(", re.I),
     "FILTER(ALL(...), ...) iterates the whole table; prefer a boolean filter argument in CALCULATE."),
    ("DAX003", "info", re.compile(r"\bFILTER\s*\(\s*'?[\w ]+'?\s*,\s*[^,()]*\[[^\]]+\]\s*(=|<>|<|>)\s*[^,()]+\)\s*\)?", re.I),
     "FILTER over a whole table with a simple column predicate; a CALCULATE boolean filter is usually faster."),
    ("DAX004", "info", re.compile(r"\bEARLIER\s*\(", re.I),
     "EARLIER is legacy; use variables."),
    ("DAX005", "medium", re.compile(r"\bCALCULATE\s*\(\s*CALCULATE\s*\(", re.I),
     "Nested CALCULATE; merge the filter arguments into one CALCULATE."),
    ("DAX006", "info", re.compile(r"\bSUMX\s*\(\s*'?[\w ]+'?\s*,\s*'?[\w ]+'?\[[^\]]+\]\s*\)", re.I),
     "SUMX over a single column; SUM is equivalent and cheaper."),
    ("DAX007", "medium", re.compile(r"\bCOUNTROWS\s*\(\s*FILTER\s*\(", re.I),
     "COUNTROWS(FILTER(...)); CALCULATE(COUNTROWS(...), predicate) is usually cheaper."),
    ("DAX008", "info", re.compile(r"\bLOOKUPVALUE\s*\(", re.I),
     "LOOKUPVALUE in a measure/column; a relationship plus RELATED is faster and clearer."),
    ("DAX009", "medium", re.compile(r"\bFORMAT\s*\(\s*[^,()]*\[[^\]]+\]\s*,\s*\"[^\"]*\"\s*\)", re.I),
     "FORMAT returns text, which breaks sorting and numeric aggregation; use a format string."),
    ("DAX010", "info", re.compile(r"\bSELECTEDVALUE\s*\(\s*[^,()]+\)\s*=\s*BLANK\s*\(\s*\)", re.I),
     "Compare with ISBLANK or give SELECTEDVALUE an alternate result."),
]


def lint(expr: str) -> list[Lint]:
    masked_strings = mask(expr)
    out: list[Lint] = []
    for rule, sev, pat, msg in _RULES:
        if pat.search(masked_strings):
            out.append(Lint(rule, sev, msg))
    if re.search(r"(?<![/*])/(?![/*])", masked_strings) and not re.search(r"\bDIVIDE\s*\(", masked_strings, re.I):
        if re.search(r"\]\s*/\s*[\w(\[']", masked_strings):
            out.append(Lint("DAX011", "medium", "Division with '/'; DIVIDE handles zero and blank safely."))
    depth = max_depth(masked_strings)
    if depth > 8:
        out.append(Lint("DAX012", "info", f"Parenthesis nesting depth {depth}; consider VAR to flatten."))
    if "VAR" not in masked_strings.upper() and len(expr) > 400:
        out.append(Lint("DAX013", "info", "Long expression with no variables; VAR improves readability and avoids recomputation."))
    return out


def max_depth(text: str) -> int:
    d = m = 0
    for ch in text:
        if ch == "(":
            d += 1
            m = max(m, d)
        elif ch == ")":
            d -= 1
    return m


def balanced(expr: str) -> list[str]:
    """Cheap syntax sanity: balanced (), [], quotes and block comments. Returns problems (empty = ok)."""
    problems: list[str] = []
    masked, open_string, open_block = _scan(expr)
    if masked.count("(") != masked.count(")"):
        problems.append("unbalanced parentheses")
    if open_string:
        problems.append("unterminated string literal")
    if open_block:
        problems.append("unterminated /* comment")
    # [..] contents are not blanked in `masked`; strip them before counting braces
    flat = _blank_brackets(masked)
    if flat.count("{") != flat.count("}"):
        problems.append("unbalanced braces")
    if re.search(r"\[(?:[^\]]|\]\])*$", masked):
        problems.append("unbalanced square brackets")
    return problems
