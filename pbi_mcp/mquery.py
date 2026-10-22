"""Best-effort structural parser for Power Query M code.

Turns a raw `let ... in ...` blob (what the engine returns for a table's
load query or a shared/staging query) into an ordered list of named steps,
each with its own formula text -- the same breakdown Power BI Desktop's own
Applied Steps pane shows, but derived from the M source text rather than
live evaluation.

Important limitation, and the reason this is a *text* parser rather than a
live evaluator: the Analysis Services engine our plugin talks to (engine.py)
only exposes the tabular model -- already-loaded, final tables. The M
("mashup") engine that actually executes these query steps runs in a
separate Power BI Desktop process with no local query API, so there is no
way for this plugin to fetch the actual intermediate *data* at each step
the way the Power Query Editor's step preview does. What this module gives
you instead is the structural breakdown: step names in order, each step's
formula, and which prior steps it references -- enough to see exactly which
step to look at, read what it does, and reason about where a value would
diverge, without needing to re-run the mashup engine.

This is a heuristic scanner, not a full M-language parser: it tracks string
literals and comments so commas/parens/`=` inside them don't confuse step
splitting, but does not tokenize the rest of the grammar. It's built to
degrade gracefully -- on anything it can't confidently split, it returns
what it has plus the untouched raw text, never raises out of parse_m().
"""
from __future__ import annotations

import re
from typing import Any

_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")", "]", "}"}


def _compute_live_mask(code: str) -> list[bool]:
    """True at each index that's real M syntax; False inside string
    literals and comments, so a depth/keyword scan can skip over them.
    """
    n = len(code)
    live = [True] * n
    i = 0
    while i < n:
        c = code[i]
        if c == '"':
            j = i + 1
            while j < n:
                if code[j] == '"':
                    if j + 1 < n and code[j + 1] == '"':  # M escapes " as ""
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                live[k] = False
            i = j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            j = i
            while j < n and code[j] != "\n":
                j += 1
            for k in range(i, j):
                live[k] = False
            i = j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = i + 2
            while j < n - 1 and not (code[j] == "*" and code[j + 1] == "/"):
                j += 1
            j = min(j + 2, n)
            for k in range(i, j):
                live[k] = False
            i = j
            continue
        i += 1
    return live


def _is_word_at(code: str, live: list[bool], i: int, word: str) -> bool:
    n = len(code)
    if not live[i] or code[i : i + len(word)] != word:
        return False
    before_ok = i == 0 or not (code[i - 1].isalnum() or code[i - 1] == "_")
    after_idx = i + len(word)
    after_ok = after_idx >= n or not (code[after_idx].isalnum() or code[after_idx] == "_")
    return before_ok and after_ok


def _find_keyword(code: str, live: list[bool], keyword: str, start: int = 0) -> int:
    """Find the first whole-word occurrence of `keyword` at a live
    position. Returns -1 if not found."""
    i = start
    n = len(code)
    while i < n:
        if _is_word_at(code, live, i, keyword):
            return i
        i += 1
    return -1


def _find_matching_in(code: str, live: list[bool], start: int) -> int:
    """`start` is the index right after the outer `let` keyword's
    statement-list begins. M's `let`/`in` nesting isn't reflected in
    bracket depth -- a nested let can sit directly inside an if/then/else
    branch with no enclosing brackets at all -- so finding the *matching*
    `in` requires counting nested let/in keyword pairs, not bracket depth.
    Returns -1 if no matching `in` is found.
    """
    nest = 1
    i = start
    n = len(code)
    while i < n:
        if _is_word_at(code, live, i, "let"):
            nest += 1
            i += 3
            continue
        if _is_word_at(code, live, i, "in"):
            nest -= 1
            if nest == 0:
                return i
            i += 2
            continue
        i += 1
    return -1


def _split_top_level(text: str, live: list[bool], sep: str) -> list[tuple[int, int]]:
    """Split `text` (with matching `live` mask, same length) on `sep` at
    bracket depth 0. Returns [(start, end), ...] spans, sep excluded.
    """
    spans = []
    depth = 0
    start = 0
    n = len(text)
    i = 0
    while i < n:
        if live[i]:
            c = text[i]
            if c in _OPEN_TO_CLOSE:
                depth += 1
            elif c in _CLOSERS:
                depth -= 1
            elif depth == 0 and c == sep:
                spans.append((start, i))
                start = i + 1
        i += 1
    spans.append((start, n))
    return spans


def _strip_function_signature(code: str, live: list[bool]) -> tuple[str | None, str, list[bool]]:
    """M shared queries are sometimes functions: `(params) as type => body`.
    Detect and strip that prefix so the body can be parsed as a normal
    let/expression. Returns (params_text_or_None, remaining_code, remaining_live).
    """
    stripped = code.lstrip()
    lead_ws = len(code) - len(stripped)
    if not stripped.startswith("("):
        return None, code, live

    depth = 0
    i = lead_ws
    n = len(code)
    while i < n:
        if live[i]:
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    close_idx = i
                    break
        i += 1
    else:
        return None, code, live

    arrow_idx = code.find("=>", close_idx)
    if arrow_idx == -1:
        return None, code, live
    # Reject if anything but "as <type>" sits between the params and "=>" --
    # a stray "=>" elsewhere would otherwise be misread as the signature end.
    between = code[close_idx + 1 : arrow_idx].strip()
    if between and not re.fullmatch(r"as\s+\w+", between):
        return None, code, live

    params = code[lead_ws + 1 : close_idx].strip()
    body_start = arrow_idx + 2
    return params, code[body_start:], live[body_start:]


def _strip_leading_comments(text: str) -> str:
    """A step name may be preceded by `//` or `/* */` comments (which belong
    to the gap between statements); drop them so the name is just the name."""
    t = text.lstrip()
    while True:
        if t.startswith("//"):
            nl = t.find("\n")
            t = "" if nl == -1 else t[nl + 1 :].lstrip()
        elif t.startswith("/*"):
            end = t.find("*/")
            t = "" if end == -1 else t[end + 2 :].lstrip()
        else:
            return t.strip()


def parse_m(code: str) -> dict[str, Any]:
    """Parse one M query into a structural breakdown.

    Returns a dict always containing `raw` (the original text) and `parsed`
    (bool). When `parsed` is True, also includes:
      - is_function / parameters: set when the query is a function
        (`(params) as type => body`) rather than a plain query
      - steps: [{"name": ..., "formula": ..., "references": [...]}, ...]
        in source order; `references` lists which *other step names in this
        same query* appear in the formula, i.e. what feeds this step
      - result: the trailing `in <expr>` text -- usually just the name of
        the final step, but can be an arbitrary expression
    When `parsed` is False, only `raw` and an `error` explaining why this
    fell back to raw text (not a real M syntax error -- just this parser's
    limits) are included.
    """
    if not code or not code.strip():
        return {"raw": code, "parsed": False, "error": "empty query"}

    try:
        live = _compute_live_mask(code)
        params, body, body_live = _strip_function_signature(code, live)

        let_idx = _find_keyword(body, body_live, "let")
        if let_idx == -1:
            # Not every query has a let block (e.g. a bare expression, or a
            # function whose body is just an if/error expression) -- that's
            # not a failure, just nothing to break into steps.
            return {
                "raw": code,
                "parsed": True,
                "is_function": params is not None,
                "parameters": params,
                "steps": [],
                "result": body.strip(),
            }

        stmts_start = let_idx + 3
        in_idx = _find_matching_in(body, body_live, stmts_start)
        if in_idx == -1:
            return {"raw": code, "parsed": False, "error": "found 'let' with no matching top-level 'in'"}

        stmts_text = body[stmts_start:in_idx]
        stmts_live = body_live[stmts_start:in_idx]
        result_text = body[in_idx + 2 :].strip()

        steps = []
        step_names = []
        for span_start, span_end in _split_top_level(stmts_text, stmts_live, ","):
            stmt = stmts_text[span_start:span_end]
            stmt_live = stmts_live[span_start:span_end]
            if not stmt.strip():
                continue
            eq_spans = _split_top_level(stmt, stmt_live, "=")
            if len(eq_spans) < 2:
                # No top-level '=' -- malformed statement or a parser miss.
                # Keep going rather than aborting the whole query.
                steps.append({"name": None, "formula": stmt.strip(), "references": [], "note": "could not split name from formula"})
                continue
            name = _strip_leading_comments(stmt[eq_spans[0][0] : eq_spans[0][1]])
            formula = stmt[eq_spans[1][0] :].strip()
            name = name[2:-1] if name.startswith('#"') and name.endswith('"') else name
            steps.append({"name": name, "formula": formula, "references": []})
            step_names.append(name)

        # Fill in cross-references now that all step names are known.
        # Word-boundary only (not quote-boundary): a reference is written
        # either bare (Sorted) or quoted (#"Added Custom"), and in the
        # quoted form the char right at the name's edge is the quote mark
        # itself -- excluding "\"" here too would reject exactly that case.
        name_pattern = {n: re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)") for n in step_names if n}
        for step in steps:
            if not step["name"]:
                continue
            refs = []
            for other in step_names:
                if other == step["name"] or not other:
                    continue
                if name_pattern[other].search(step["formula"]):
                    refs.append(other)
            step["references"] = refs

        return {
            "raw": code,
            "parsed": True,
            "is_function": params is not None,
            "parameters": params,
            "steps": steps,
            "result": result_text,
        }
    except Exception as exc:  # noqa: BLE001 -- this parser must never crash a tool call
        return {"raw": code, "parsed": False, "error": f"parser error: {exc}"}


# Common M data-source connector functions. Not exhaustive -- M has dozens of
# connectors, including custom ones from third-party extensions this list
# can't know about -- but covers the connectors seen in practice for import-
# mode Power BI Desktop models (database, file, web, and cloud-service
# sources). A query using a connector not in this list simply won't surface
# in find_data_source_calls(); it isn't miscategorized, just not found.
DATA_SOURCE_FUNCTIONS = (
    "Sql.Database", "Sql.Databases",
    "AnalysisServices.Database", "AnalysisServices.Databases",
    "SharePoint.Files", "SharePoint.Tables", "SharePoint.Contents",
    "Web.Contents", "Web.Page",
    "Odbc.DataSource", "Odbc.Query",
    "OleDb.DataSource",
    "Excel.Workbook", "Excel.CurrentWorkbook",
    "Folder.Files", "Folder.Contents",
    "OData.Feed",
    "PostgreSQL.Database", "MySQL.Database", "Oracle.Database",
    "Snowflake.Databases", "Teradata.Database", "SapHana.Database",
    "GoogleAnalytics.Accounts", "Salesforce.Data", "Salesforce.Reports",
    "AzureStorage.BlobContents", "AzureStorage.Tables",
    "Dynamics365.Contents", "CommonDataService.Database", "DataverseModel.Database",
    "Exchange.Contents", "Facebook.Graph",
    # File.Contents is a local/synced-folder path (e.g. an OneDrive-synced
    # C:\Users\... file): it works on this PC and fails on any other machine
    # or in the Power BI service, so it's a connection worth surfacing.
    "File.Contents", "Web.BrowserContents",
    "SharePoint.Files",
)


def find_data_source_calls(code: str) -> list[dict[str, Any]]:
    """Scan M source text for calls to known data-source connector
    functions, returning each call's function name and a preview of its
    string-literal arguments (typically the server/URL/path). Used to build
    a "what does this model actually connect to" inventory across every
    table and shared query, since there's no local Analysis Services DMV
    that reliably lists modern Power Query data sources for import-mode
    models (only classic multidimensional/DirectQuery sources show up in
    $SYSTEM.TMSCHEMA_DATA_SOURCES).
    """
    if not code:
        return []

    live = _compute_live_mask(code)
    n = len(code)
    results = []
    seen_funcs: set[str] = set()

    for func in DATA_SOURCE_FUNCTIONS:
        if func in seen_funcs:
            continue  # DATA_SOURCE_FUNCTIONS has an intentional duplicate; skip re-scanning it
        seen_funcs.add(func)

        search_from = 0
        while True:
            idx = code.find(func, search_from)
            if idx == -1:
                break
            search_from = idx + len(func)
            if not _is_word_at(code, live, idx, func):
                continue

            j = idx + len(func)
            while j < n and live[j] and code[j] in " \t\r\n":
                j += 1
            if j >= n or not live[j] or code[j] != "(":
                continue

            depth = 0
            k = j
            close = -1
            while k < n:
                if live[k]:
                    if code[k] == "(":
                        depth += 1
                    elif code[k] == ")":
                        depth -= 1
                        if depth == 0:
                            close = k
                            break
                k += 1
            args_text = code[j : close + 1] if close != -1 else code[j : j + 300]

            literals = [lit.replace('""', '"') for lit in re.findall(r'"((?:[^"]|"")*)"', args_text)]
            literals = [(lit if len(lit) <= 200 else lit[:200] + "...") for lit in literals]
            # Parameterised sources (Sql.Database(ServerParam, DbParam)) have
            # no string literals; keep the raw argument text so they stay
            # distinguishable instead of all collapsing into one blank entry.
            raw_args = " ".join(args_text.split())[:200]
            results.append({"function": func, "arguments_preview": literals[:4], "arguments_raw": raw_args})

    return results


# ---------------------------------------------------------------------------
# Call/argument primitives used by the connection, SQL, folding, SharePoint
# and dependency analyzers (analysis.py). Same heuristic-scanner philosophy as
# parse_m: string/comment aware, never raises.
# ---------------------------------------------------------------------------

def iter_calls(code: str, names) -> list[dict[str, Any]]:
    """Every live call `Name( ... )` to any function in `names`, in source
    order: {function, index, args_text} where args_text includes the parens."""
    if not code:
        return []
    live = _compute_live_mask(code)
    n = len(code)
    out: list[dict[str, Any]] = []
    for func in set(names):
        start = 0
        while True:
            idx = code.find(func, start)
            if idx == -1:
                break
            start = idx + len(func)
            if not _is_word_at(code, live, idx, func):
                continue
            j = idx + len(func)
            while j < n and live[j] and code[j] in " \t\r\n":
                j += 1
            if j >= n or not live[j] or code[j] != "(":
                continue
            depth, k, close = 0, j, -1
            while k < n:
                if live[k]:
                    if code[k] == "(":
                        depth += 1
                    elif code[k] == ")":
                        depth -= 1
                        if depth == 0:
                            close = k
                            break
                k += 1
            args_text = code[j : close + 1] if close != -1 else code[j : j + 4000]
            out.append({"function": func, "index": idx, "args_text": args_text})
    out.sort(key=lambda c: c["index"])
    return out


def split_call_args(args_text: str) -> list[str]:
    """Split "(a, b, [opt = 1])" into top-level argument strings."""
    inner = (args_text or "").strip()
    if inner.startswith("("):
        inner = inner[1:]
    if inner.endswith(")"):
        inner = inner[:-1]
    if not inner.strip():
        return []
    live = _compute_live_mask(inner)
    return [inner[a:b].strip() for a, b in _split_top_level(inner, live, ",") if inner[a:b].strip()]


_STR_LITERAL = re.compile(r'^"((?:[^"]|"")*)"$', re.S)


def string_literal_value(expr: str) -> str | None:
    """The text of `expr` if it is exactly one M string literal, else None."""
    m = _STR_LITERAL.match((expr or "").strip())
    return m.group(1).replace('""', '"') if m else None


def expr_to_text(expr: str) -> tuple[str | None, bool]:
    """Best-effort text of an M expression that builds a string: returns
    (text, dynamic). A pure literal gives (text, False). A concatenation
    like "a" & Param & "b" gives ("a?b", True), with every non-literal part
    replaced by "?". Anything else is (None, True)."""
    expr = (expr or "").strip()
    lit = string_literal_value(expr)
    if lit is not None:
        return lit, False
    live = _compute_live_mask(expr)
    parts = [expr[a:b].strip() for a, b in _split_top_level(expr, live, "&")]
    if len(parts) < 2:
        return None, True
    texts, any_literal = [], False
    for p in parts:
        v = string_literal_value(p)
        if v is None:
            texts.append("?")
        else:
            texts.append(v)
            any_literal = True
    return ("".join(texts), True) if any_literal else (None, True)


def parse_record_options(expr: str) -> dict[str, str]:
    """Parse an M record literal "[A = 1, B = "x"]" into {"A": "1", "B": '"x"'}."""
    expr = (expr or "").strip()
    if not (expr.startswith("[") and expr.endswith("]")):
        return {}
    inner = expr[1:-1]
    live = _compute_live_mask(inner)
    opts: dict[str, str] = {}
    for a, b in _split_top_level(inner, live, ","):
        field = inner[a:b]
        if "=" not in field:
            continue
        k, v = field.split("=", 1)
        opts[k.strip().strip('#"').strip('"')] = v.strip()
    return opts


_NATIVE_OPTION_FUNCS = (
    "Sql.Database", "Oracle.Database", "PostgreSQL.Database", "MySQL.Database",
    "Teradata.Database", "SapHana.Database", "Snowflake.Databases",
)
_NATIVE_POSITIONAL_FUNCS = ("Value.NativeQuery", "Odbc.Query", "OleDb.Query")


def find_native_queries(code: str) -> list[dict[str, Any]]:
    """Every hand-written SQL statement embedded in the M code: a `[Query=...]`
    option on a database connector, or the SQL argument of Value.NativeQuery /
    Odbc.Query. Returns {function, sql (None if built from a variable),
    dynamic (built by concatenation), enable_folding, connection_args}."""
    results: list[dict[str, Any]] = []
    for c in iter_calls(code, _NATIVE_OPTION_FUNCS + _NATIVE_POSITIONAL_FUNCS):
        args = split_call_args(c["args_text"])
        func = c["function"]
        sql, dynamic, folding = None, False, None
        conn_args = [v for v in (string_literal_value(a) for a in args[:2]) if v is not None]
        if func in _NATIVE_OPTION_FUNCS:
            opts: dict[str, str] = {}
            for a in args:
                opts.update(parse_record_options(a))
            if "Query" not in opts:
                continue
            sql, dynamic = expr_to_text(opts["Query"])
        else:
            if len(args) < 2:
                continue
            sql, dynamic = expr_to_text(args[1])
            for a in args[2:]:
                o = parse_record_options(a)
                if "EnableFolding" in o:
                    folding = o["EnableFolding"].strip().lower() == "true"
        results.append(
            {"function": func, "sql": sql, "dynamic": dynamic, "enable_folding": folding, "connection_args": conn_args}
        )
    return results


def find_query_refs(code: str, names, ignore=()) -> set[str]:
    """Which of `names` (other queries/functions/parameters) does this M text
    reference? Handles both #"Quoted Name" and bare identifiers, ignores
    anything inside strings/comments, record-field labels, `[Field]` access,
    and names in `ignore` (this query's own step/parameter names, which
    shadow a same-named query)."""
    if not code:
        return set()
    live = _compute_live_mask(code)
    found: set[str] = set()
    for nm in names:
        if nm in ignore or not nm:
            continue
        q = '#"' + nm.replace('"', '""') + '"'
        pos = code.find(q)
        hit = False
        while pos != -1:
            if live[pos]:
                hit = True
                break
            pos = code.find(q, pos + 1)
        if hit:
            found.add(nm)
            continue
        if re.fullmatch(r"[A-Za-z_][\w.]*", nm):
            for m in re.finditer(r'(?<![\w.#"\[])' + re.escape(nm) + r'(?![\w."])(?!\s*=(?![=>]))', code):
                if live[m.start()]:
                    found.add(nm)
                    break
    return found


_PARAM_META = re.compile(r"^\s*(?P<value>.+?)\s+meta\s*\[(?P<meta>.*)\]\s*$", re.S)


def parse_parameter(expression: str) -> dict[str, Any] | None:
    """If a shared expression is a Power Query *parameter* ("value" meta
    [IsParameterQuery=true, ...]), return {value, raw}; else None."""
    m = _PARAM_META.match(expression or "")
    if not m or "IsParameterQuery" not in m.group("meta"):
        return None
    raw = m.group("value").strip()
    lit = string_literal_value(raw)
    return {"value": lit if lit is not None else raw, "raw": raw}
