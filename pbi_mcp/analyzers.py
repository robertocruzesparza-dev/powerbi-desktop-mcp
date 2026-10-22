"""Static analyzers over the Power Query (M) layer: connection classification,
embedded-SQL review, query-folding risk, SharePoint loading patterns, and the
refresh dependency graph.

Everything here reads M *source text*. It can say how a query is wired and
where a pattern is risky; it cannot observe runtime behavior. Where a
conclusion depends on runtime behavior that we could not verify (notably
SharePoint folding/paging, which Query Diagnostics does not expose), the
output says so instead of guessing.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from . import mquery, qmodel
from .qmodel import Q

_SEV_ORDER = {"high": 0, "medium": 1, "info": 2}


def _finding(sev: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"severity": sev, "code": code, "message": message, **extra}


def _summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(f["severity"] for f in findings)
    return {"high": c.get("high", 0), "medium": c.get("medium", 0), "info": c.get("info", 0), "total": len(findings)}


def _sorted(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda f: _SEV_ORDER.get(f["severity"], 9))


# ============================================================================
# 1. Connection classification
# ============================================================================

def classify_connections(queries: dict[str, Q]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for q in queries.values():
        for c in q.classified:
            g = groups.setdefault(
                c["key"],
                {"key": c["key"], "kind": c["kind"], "display": c["display"], "functions": set(), "gateway": c.get("gateway"),
                 "raw_variants": set(), "flags": {}, "used_by": []},
            )
            g["functions"].add(c["function"])
            g["raw_variants"].add(" / ".join(c["identity"]) if c["identity"] else c["display"])
            for fl in c["flags"]:
                g["flags"].setdefault(fl["code"], fl)
            tag = f"{q.scope}:{q.name}"
            if tag not in g["used_by"]:
                g["used_by"].append(tag)

    findings: list[dict[str, Any]] = []
    connections = []
    for g in groups.values():
        for fl in g["flags"].values():
            findings.append(_finding(fl["severity"], fl["code"], fl["message"], connection=g["display"], kind=g["kind"], used_by=g["used_by"]))
        connections.append(
            {
                "kind": g["kind"], "connection": g["display"], "functions": sorted(g["functions"]),
                "gateway_required": g["gateway"], "used_by": g["used_by"],
                "flags": [{"severity": f["severity"], "code": f["code"]} for f in g["flags"].values()],
            }
        )

    # Same target written more than one way (case, trailing slash, http/https...)
    by_norm: dict[tuple[str, str], set[str]] = defaultdict(set)
    for q in queries.values():
        for c in q.classified:
            if c["kind"] in ("sharepoint_site", "onedrive_personal", "sharepoint_web_file", "onedrive_web_file"):
                for u in (c.get("url_variants") or []):
                    by_norm[(c["kind"].split("_")[0], c["site"])].add(u)
            elif c["kind"] == "sql_server":
                by_norm[("sql", f"{c['server'].lower()}/{c['database'].lower()}")].add(" / ".join(c["identity"]))
    for (_k, norm), variants in by_norm.items():
        if len(variants) > 1:
            findings.append(_finding("medium", "connection_written_multiple_ways",
                                     f"The same target ({norm}) is spelled {len(variants)} different ways: {sorted(variants)}. Power BI treats each spelling as a separate data source (separate credentials/privacy settings), and one variant can fail while the others work.",
                                     connection=norm, variants=sorted(variants)))

    param_uses = []
    for q in queries.values():
        if q.is_parameter:
            users = sorted(x.name for x in queries.values() if q.name in x.refs)
            param_uses.append({"parameter": q.name, "value": q.param_value, "used_by": users})

    gateway = sorted({g["display"] for g in groups.values() if g["gateway"]})
    return {
        "connections": sorted(connections, key=lambda c: (c["kind"], c["connection"])),
        "connection_count": len(connections),
        "requires_gateway_for_service_refresh": gateway,
        "parameters": param_uses,
        "findings": _sorted(findings),
        "summary": _summary(findings),
    }


# ============================================================================
# 2. Embedded SQL review
# ============================================================================

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_STR = re.compile(r"'(?:[^']|'')*'")
_SQL_BRACKET = re.compile(r"\[[^\]]*\]")


def analyze_sql_text(sql: str, connection_db: str | None = None) -> dict[str, Any]:
    """Lint one SQL statement/batch. Heuristic (regex over comment/string-
    stripped text): every finding names the evidence so it can be checked."""
    s = _SQL_COMMENT.sub(" ", sql or "")
    nostr = _SQL_STR.sub("''", s)
    t = _SQL_BRACKET.sub("[]", nostr).lower()
    findings: list[dict[str, Any]] = []

    def add(sev, code, msg, evidence=None):
        d = _finding(sev, code, msg)
        if evidence:
            d["evidence"] = evidence
        findings.append(d)

    # --- writes / side effects in a read query -------------------------------
    def temp(target: str) -> bool:
        return target.startswith(("#", "@"))

    writes: list[str] = []
    for m in re.finditer(r"\bdrop\s+(?:table|view|procedure|proc|database|index|function)\s+(?:if\s+exists\s+)?([#@\w.\[\]]+)", t):
        if not temp(m.group(1)):
            writes.append(f"DROP {m.group(1)}")
    for m in re.finditer(r"\binsert\s+into\s+([#@\w.\[\]]+)", t):
        if not temp(m.group(1)):
            writes.append(f"INSERT INTO {m.group(1)}")
    for m in re.finditer(r"\bupdate\s+([#@\w.\[\]]+)\s+set\b", t):
        if not temp(m.group(1)):
            writes.append(f"UPDATE {m.group(1)}")
    for m in re.finditer(r"\bdelete\s+(?:from\s+)?([#@\w.\[\]]+)", t):
        if not temp(m.group(1)) and m.group(1) not in ("from",):
            writes.append(f"DELETE {m.group(1)}")
    for m in re.finditer(r"\b(truncate\s+table|merge\s+(?:into\s+)?|alter\s+(?:table|view|procedure|database))\s*([#@\w.\[\]]*)", t):
        if not temp(m.group(2)):
            writes.append(re.sub(r"\s+", " ", m.group(0)).upper().strip())
    for m in re.finditer(r"\bcreate\s+(?:table|view|index|procedure|proc|function|database)\s+([#@\w.\[\]]+)", t):
        if not temp(m.group(1)):
            writes.append(f"CREATE {m.group(1)}")
    for m in re.finditer(r"\binto\s+([#@\w.\[\]]+)", t):
        before = t[max(0, m.start() - 12): m.start()]
        if not re.search(r"insert\s*$", before) and not temp(m.group(1)):
            writes.append(f"SELECT ... INTO {m.group(1)} (creates a permanent table)")
    if writes:
        add("high", "sql_writes_data",
            "The query modifies or creates non-temporary database objects. Import queries run on EVERY refresh (and again for each preview/evaluation), so these statements execute repeatedly, against the live database.",
            sorted(set(writes))[:8])
    execs = re.findall(r"\bexec(?:ute)?\s+([\w.\[\]@]+)", t)
    if execs:
        add("medium", "sql_executes_procedure", "Executes a stored procedure / dynamic SQL; its side effects can't be seen from here.", sorted(set(execs))[:6])

    # --- rows silently dropped or over-fetched -------------------------------
    tops = [int(n) for n, pct in re.findall(r"\btop\s*\(?\s*(\d+)\s*\)?(\s*percent)?", t) if not pct]
    if tops:
        add("medium", "sql_top_n_cap",
            f"TOP {sorted(set(tops))} limits the rows returned. If this is a whole-table pull (or a subquery feeding a join) older/extra rows are silently excluded; confirm the cap is intentional and still large enough.",
            [f"TOP {n}" for n in sorted(set(tops))])
    if re.search(r"\bselect\s+(?:all\s+|distinct\s+)?(?:top\s*\(?\s*\d+\s*\)?\s+)?(?:[#\w\[\]]+\.)?\*", t):
        add("medium", "sql_select_star", "SELECT * pulls every column, including ones the report never uses, and new source columns appear silently (they can break later steps).")
    if re.search(r"\bfrom\b", t) and not re.search(r"\bwhere\b", t):
        add("info", "sql_no_where", "No WHERE clause anywhere: the query returns every row of its source table(s).")

    # --- hard-coded years ----------------------------------------------------
    idents = sorted(set(re.findall(r"\b[a-z][a-z_]*_(?:19|20)?\d{2}(?:\d{2})?\b", nostr.lower())))
    year_idents = [i for i in idents if re.search(r"_(?:\d{4}|\d{2})$", i) and re.search(r"\d{4}$", i)]
    if year_idents:
        add("medium", "sql_year_specific_tables",
            "Year-specific table names: every new year needs a manual edit, and a year not listed is silently missing from the model.",
            year_idents[:10])
    yr_lits = sorted(set(re.findall(r"'((?:19|20)\d{2}|\d{4})'", s)))
    if yr_lits:
        add("medium", "sql_hardcoded_year_literals", f"Hard-coded year/period value(s) {yr_lits[:8]}: these go stale when the year rolls over.")

    # --- silent truncation ---------------------------------------------------
    trunc = len(re.findall(r"\bas\s+(?:n?varchar|n?char)\s*\)", t)) + len(re.findall(r"\bconvert\s*\(\s*(?:n?varchar|n?char)\s*,", t))
    if trunc:
        add("medium", "sql_cast_without_length",
            f"{trunc} CAST/CONVERT to varchar/nvarchar without a length: SQL Server defaults to 30 characters here and silently truncates longer text. Fine for short codes/numbers, wrong for names or descriptions.")

    # --- informational -------------------------------------------------------
    if re.search(r"\bnolock\b", t):
        add("info", "sql_nolock", "NOLOCK reads uncommitted data: rows can be missing or duplicated during concurrent writes.")
    if re.search(r"\bunion\b(?!\s+all\b)", t):
        add("info", "sql_union_distinct", "UNION (without ALL) de-duplicates the combined rows, which costs a sort and can hide legitimate duplicate rows.")
    no_over = re.sub(r"over\s*\([^()]*\)", " ", t)
    if re.search(r"\border\s+by\b", no_over):
        add("info", "sql_order_by", "ORDER BY costs a server-side sort, and Power BI import doesn't guarantee to preserve the order.")
    temps = sorted(set(re.findall(r"#\w+", t)))
    if temps:
        add("info", "sql_temp_tables", f"Multi-statement batch using temp tables {temps[:6]}: cannot fold, and the whole batch re-runs on every evaluation.")

    dbs = set()
    for m in re.finditer(r"\b\[?(\w+)\]?\.\[?(\w+)\]?\.\[?(\w+)\]?\b", nostr):
        db = m.group(1)
        if db.lower() not in ("dbo", "sys", "tempdb", "information_schema") and not db.startswith("@"):
            dbs.add(db)
    other = sorted(d for d in dbs if not connection_db or d.lower() != connection_db.lower())
    if other:
        add("info", "sql_cross_database", f"Reads from other database(s) than the connection's ({connection_db or 'n/a'}): {other[:6]}. Needs permission on each and the same server.")

    metrics = {
        "characters": len(sql or ""),
        "joins": len(re.findall(r"\bjoin\b", t)),
        "tables_referenced": sorted(set(re.findall(r"\b(?:from|join)\s+([#\w.\[\]]+)", nostr.lower())))[:40],
    }
    return {"metrics": metrics, "findings": _sorted(findings)}


def analyze_sql_queries(queries: dict[str, Q]) -> dict[str, Any]:
    out, nav, all_findings = [], [], []
    for q in queries.values():
        sql_sources = [c for c in qmodel.all_sources(queries, q.name).values() if c["kind"] == "sql_server"]
        db = sql_sources[0]["database"] if sql_sources else None
        for n in q.native:
            if n["sql"] is None:
                item = {"query": q.name, "scope": q.scope, "loaded": q.loaded, "function": n["function"], "sql_preview": None,
                        "findings": [_finding("info", "sql_built_from_variable", "The SQL text comes from a variable/expression, so it can't be reviewed statically.")]}
            else:
                res = analyze_sql_text(n["sql"], db)
                fl = res["findings"]
                if n["dynamic"]:
                    fl = [_finding("info", "sql_dynamic", "The SQL is assembled by concatenation ('?' marks the inserted parts): confirm values are parameters, not user-entered text (SQL injection / quoting bugs).")] + fl
                item = {"query": q.name, "scope": q.scope, "loaded": q.loaded, "function": n["function"], "enable_folding": n["enable_folding"],
                        "connection": sql_sources[0]["display"] if sql_sources else None, "metrics": res["metrics"],
                        "sql_preview": " ".join(n["sql"].split())[:300], "findings": fl}
            for f in item["findings"]:
                all_findings.append({**f, "query": q.name})
            out.append(item)
        if not q.native and any(c["kind"] == "sql_server" for c in q.classified):
            tables = re.findall(r'\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]', q.text)
            views = re.findall(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"(?:Table|View)"\s*\]', q.text)
            nav.append({"query": q.name, "loaded": q.loaded, "tables": [f"{a}.{b}" for a, b in tables] + views})
    return {
        "native_sql_queries": out,
        "navigation_sql_queries": nav,
        "note": "Navigation queries (Sql.Database without [Query=...]) can fold later steps into SQL; native queries cannot. See analyze_folding_risk.",
        "findings": _sorted(all_findings),
        "summary": _summary(all_findings),
    }


# ============================================================================
# 3. Query folding risk
# ============================================================================

_FOLDS = {
    "Table.SelectRows", "Table.SelectColumns", "Table.RemoveColumns", "Table.RenameColumns", "Table.ReorderColumns",
    "Table.Sort", "Table.FirstN", "Table.Distinct", "Table.Group", "Table.TransformColumnTypes", "Table.Join",
    "Table.NestedJoin", "Table.ExpandTableColumn", "Table.Skip", "Table.RowCount",
}
_CONDITIONAL = {
    "Table.AddColumn", "Table.TransformColumns", "Table.ReplaceValue", "Table.DuplicateColumn", "Table.Combine",
    "Table.RemoveRowsWithErrors", "Table.MaxN", "Table.MinN", "Table.RemoveFirstN", "Table.Range",
}
_BREAKS = {
    "Table.Buffer", "List.Buffer", "Table.AddIndexColumn", "Table.FillDown", "Table.FillUp", "Table.Pivot", "Table.Unpivot",
    "Table.UnpivotOtherColumns", "Table.Transpose", "Table.PromoteHeaders", "Table.DemoteHeaders", "Table.SplitColumn",
    "Table.CombineColumns", "Table.ExpandRecordColumn", "Table.ExpandListColumn", "Table.Profile", "Table.ToRows",
    "Table.ToColumns", "Table.FromRows", "Table.FromColumns", "Table.FromList", "Table.FromRecords", "Table.InsertRows",
    "Table.Repeat", "Table.ReplaceErrorValues", "Table.RemoveMatchingRows", "Table.RemoveLastN", "Table.LastN",
    "Table.AlternateRows", "Table.SelectRowsWithErrors", "Table.Partition", "Table.FuzzyNestedJoin", "Table.FuzzyJoin",
}
_MERGE = {"Table.NestedJoin", "Table.Join", "Table.Combine", "Table.FuzzyNestedJoin", "Table.FuzzyJoin"}
_NONSQL_EXPR = re.compile(r"\b(?:Text|List|Date|DateTime|DateTimeZone|Time|Duration|Number|Record|Table|Value|Binary|Json|Uri|Splitter|Combiner|Replacer|Comparer)\.\w+")
_LEADING_FUNC = re.compile(r"^\s*(?:try\s+)?([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(")


def _step_verdict(fn: str | None, formula: str) -> tuple[str, str]:
    if fn is None:
        return "neutral", ""
    if fn in _BREAKS:
        return "breaks", f"{fn} cannot be translated to the source query"
    if fn in _CONDITIONAL or fn == "Table.SelectRows":
        body = formula[len(fn):] if formula.strip().startswith(fn) else formula
        each = re.search(r"\beach\b(.*)", body, re.S)
        if each and _NONSQL_EXPR.search(each.group(1)):
            names = sorted(set(_NONSQL_EXPR.findall(each.group(1))))[:4]
            return "conditional", f"custom expression calls {names}, which often can't be translated to SQL"
        return ("folds", "") if fn == "Table.SelectRows" else ("conditional", "folds only for simple expressions")
    if fn in _FOLDS:
        return "folds", ""
    if fn.split(".")[0] in ("List", "Text", "Csv", "Json", "Excel", "Web", "Binary", "Record", "Xml", "Value"):
        return "breaks", f"{fn} runs in the mashup engine"
    return "unknown", f"{fn} not classified"


def _base_kind(q: Q) -> str:
    if q.native and not any(n.get("enable_folding") for n in q.native):
        return "sql_native"
    kinds = {c["kind"] for c in q.classified}
    if kinds & {"sql_server", "database"}:
        return "sql_navigation"
    if kinds & {"sharepoint_site", "onedrive_personal", "sharepoint_web_file", "onedrive_web_file"}:
        return "sharepoint"
    if kinds & {"local_file", "unc_file", "web", "embedded_workbook", "odbc", "other"}:
        return "file_or_web"
    return "none"


def analyze_folding(queries: dict[str, Q], include_steps: bool = False) -> dict[str, Any]:
    memo: dict[str, dict[str, Any]] = {}

    def profile(name: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in memo:
            return memo[name]
        q = queries[name]
        if name in stack:
            return {"base": "none", "alive": False, "keys": set(), "steps": [], "findings": []}
        own = _base_kind(q)
        keys = set(qmodel.all_sources(queries, name))
        up_alive, up_base = None, None
        primary_keys = {c["key"] for c in q.classified}
        if own == "none" and q.refs:
            first = (q.parsed.get("steps") or [{}])[0].get("formula", "") if q.parsed.get("parsed") else q.text
            cands = [r for r in sorted(q.refs) if r in queries and not queries[r].is_parameter]
            ordered = [r for r in cands if r in mquery.find_query_refs(first, [r])] or cands
            if ordered:
                up = profile(ordered[0], stack + (name,))
                up_alive, up_base = up["alive"], up["base"]
                primary_keys |= set(qmodel.all_sources(queries, ordered[0]))
        base = own if own != "none" else (up_base or "none")
        alive = {"sql_navigation": True, "sql_native": False, "sharepoint": False, "file_or_web": False}.get(own, up_alive if up_alive is not None else False)
        steps_out, findings = [], []
        broke_at, filters_after = None, []
        steps = (q.parsed.get("steps") or []) if q.parsed.get("parsed") else []
        pipeline_keys = set(primary_keys)
        for i, st in enumerate(steps):
            name_s, formula = st.get("name") or f"step {i+1}", st.get("formula", "")
            m = _LEADING_FUNC.match(formula)
            fn = m.group(1) if m else None
            # The first step is the source (a connector call, a reference to
            # another query, or a function-query call) -- not a transform.
            if i == 0 and (fn is None or fn in qmodel._SOURCE_FUNCS or fn in mquery._NATIVE_POSITIONAL_FUNCS):
                steps_out.append({"step": name_s, "function": fn, "state": "source"})
                continue
            verdict, why = _step_verdict(fn, formula)
            if fn in _MERGE:
                partner_names = [r for r in q.refs if r in queries and r in mquery.find_query_refs(formula, [r])]
                pkeys: set[str] = set()
                p_dead = False
                for pn in partner_names:
                    pkeys |= set(qmodel.all_sources(queries, pn))
                    p_dead = p_dead or not profile(pn, stack + (name,))["alive"]
                if pkeys and not pkeys <= pipeline_keys:
                    verdict, why = "breaks", "merges with a query from a different data source: both sides are fully loaded into the mashup engine and joined there"
                    findings.append(_finding("medium", "cross_source_merge",
                                             f"Step '{name_s}' merges data from different sources ({sorted(pkeys - pipeline_keys)}); the join runs locally on the full extracted data of both sides.", query=name, step=name_s))
                elif partner_names and alive and p_dead:
                    verdict, why = "breaks", "merges with a query that itself can't fold (native SQL or a non-database source)"
                pipeline_keys |= pkeys
            if fn in ("Table.Buffer", "List.Buffer"):
                findings.append(_finding("medium", "table_buffer", f"Step '{name_s}' buffers the table in memory: it forces full materialization and stops query folding.", query=name, step=name_s))
            state = "local"
            if alive:
                if verdict == "breaks":
                    state, alive, broke_at = "breaks_folding", False, broke_at or name_s
                elif verdict == "conditional":
                    state = "may_break_folding"
                elif verdict == "folds":
                    state = "folds"
                else:
                    state = "folds" if verdict == "neutral" else "unknown"
            else:
                if verdict == "neutral":
                    state = "neutral"
                # A row filter that runs locally although the source is a database:
                # the database sent every row first and Power BI then discarded some.
                if fn == "Table.SelectRows" and base in ("sql_native", "sql_navigation"):
                    filters_after.append(name_s)
            steps_out.append({"step": name_s, "function": fn, "state": state, **({"note": why} if why else {})})
        if own == "sql_native":
            findings.append(_finding("info", "native_query_no_folding",
                                     f"Source is a hand-written SQL statement: no later step can fold, so all {max(len(steps)-1,0)} following step(s) run locally over the full result. Put filters/column choices inside the SQL instead.", query=name))
        if broke_at and own == "sql_navigation":
            later = [s["step"] for s in steps_out if s["state"] in ("local", "neutral")]
            findings.append(_finding("medium", "folding_broken",
                                     f"Folding stops at step '{broke_at}'; {len(later)} later step(s) run locally over every row the database returns.", query=name, step=broke_at))
        if filters_after:
            findings.append(_finding("medium", "filter_after_folding_ended",
                                     f"Row filter(s) {filters_after} run AFTER folding ended (or against a native query), so the source sends all rows first and Power BI filters them itself. Move them earlier or into the SQL.", query=name, steps=filters_after))
        report_here = q.loaded or own != "none"   # origin queries, and loaded tables inheriting a source
        if base == "sharepoint" and report_here:
            findings.append(_finding("info", "sharepoint_folding_unverified",
                                     "SharePoint source: query folding can't be proven from M text or Query Diagnostics (which leaves Data Source Query empty for SharePoint). Treat every step as local and reduce rows/columns early; run read_mashup_traces to see the engine's own folding-failure reports and the real HTTP requests.", query=name))
        elif base == "file_or_web" and report_here:
            findings.append(_finding("info", "no_folding_for_source", "File/web/ODBC sources don't fold: every step runs in the mashup engine over the full extracted data.", query=name))
        res = {"base": base, "alive": alive, "keys": keys, "steps": steps_out, "findings": findings, "broke_at": broke_at}
        memo[name] = res
        return res

    per_query, all_findings = [], []
    for name, q in queries.items():
        if q.is_parameter or (q.is_function and not q.classified and not q.refs):
            continue
        p = profile(name)
        counts = Counter(s["state"] for s in p["steps"])
        item = {"query": name, "scope": q.scope, "loaded": q.loaded, "source_type": p["base"], "steps_total": len(p["steps"]),
                "folds": counts.get("folds", 0), "may_break": counts.get("may_break_folding", 0),
                "breaks_at": p["broke_at"], "local_steps": counts.get("local", 0)}
        if include_steps:
            item["steps"] = p["steps"]
        per_query.append(item)
        all_findings.extend(p["findings"])
    return {
        "queries": per_query,
        "findings": _sorted(all_findings),
        "summary": _summary(all_findings),
        "note": "Static estimate from M text. 'folds' means a step is the kind that normally translates to SQL and nothing earlier stopped folding; it is not proof. Verify important ones with 'View Native Query' in Power Query.",
    }


# ============================================================================
# 4. SharePoint loading patterns
# ============================================================================

_SP_SYSTEM_COLS = {
    "created", "created by", "modified", "modified by", "edit", "type", "item child count", "folder child count",
    "label setting", "retention label", "retention label applied", "label applied by", "item is a record",
    "app created by", "app modified by", "content type", "compliance asset id", "version", "attachments", "color tag",
    "path", "item type", "properties", "id", "folder path",
}
_REDUCERS = ("Table.SelectColumns", "Table.RemoveColumns", "Table.SelectRows", "Table.FirstN", "Table.Distinct")
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SILENT_TAIL = re.compile(r'(null\b|false\b|true\b|0\b|""|\{\s*\}|#table\b|Table\.FromRows|\[\s*\])')


def _is_sp(c: dict[str, Any]) -> bool:
    return c["kind"] in ("sharepoint_site", "onedrive_personal", "sharepoint_web_file", "onedrive_web_file")


def silent_failures(q: Q) -> list[dict[str, Any]]:
    out = []
    live = mquery._compute_live_mask(q.text)
    for m in re.finditer(r"\botherwise\b\s*(.{0,40})", q.text, re.S):
        if not live[m.start()]:
            continue
        if _SILENT_TAIL.match(m.group(1)):
            a = max(0, m.start() - 90)
            out.append({"query": q.name, "snippet": " ".join(q.text[a: m.start() + 50].split())})
    return out


def analyze_sharepoint(queries: dict[str, Q]) -> dict[str, Any]:
    sp_queries = {n: q for n, q in queries.items() if any(_is_sp(c) for c in qmodel.all_sources(queries, n).values())}
    findings: list[dict[str, Any]] = []
    fn_queries = {n: q for n, q in queries.items() if q.is_function and any(_is_sp(c) for c in qmodel.all_sources(queries, n).values())}

    # -- every place a list/file is pulled -----------------------------------
    pulls: dict[str, dict[str, Any]] = {}
    for q in queries.values():
        for m in re.finditer(r'\{\s*\[\s*(Id|Name|Title|Url)\s*=\s*"([^"]+)"\s*\]\s*\}\s*\[\s*(Items|Content|Data)\s*\]', q.text):
            key = f"{m.group(1)}={m.group(2)}"
            pulls.setdefault(key, {"identifier": key, "via": "direct indexing", "queries": []})["queries"].append(q.name)
        for call in mquery.iter_calls(q.text, list(fn_queries)):
            args = mquery.split_call_args(call["args_text"])
            lit = mquery.string_literal_value(args[0]) if args else None
            key = f"Id={lit}" if lit else f"(computed by {call['function']} argument)"
            pulls.setdefault(key, {"identifier": key, "via": call["function"], "queries": []})["queries"].append(q.name)
    for p in pulls.values():
        p["queries"] = sorted(set(p["queries"]))
        p["used_by_count"] = len(p["queries"])
        if p["used_by_count"] > 1 and p["identifier"].startswith("Id="):
            findings.append(_finding("medium", "same_list_loaded_multiple_times",
                                     f"SharePoint list {p['identifier']} is fetched separately by {p['queries']}: it is downloaded once per query. Load it once into a staging query and reference that.", queries=p["queries"]))

    # -- SharePoint.Files enumerations ---------------------------------------
    files_pulls = []
    for q in queries.values():
        if any(c["function"] == "SharePoint.Files" for c in q.calls):
            filt = re.search(r"Table\.SelectRows\([^)]*?each\s+[^)]*?\[(Name|Folder Path|Extension)\]", q.text, re.S)
            files_pulls.append({"query": q.name, "filters_by": filt.group(1) if filt else None})
            if filt:
                findings.append(_finding("medium", "sharepoint_files_site_wide_listing",
                                         f"'{q.name}' calls SharePoint.Files (which lists EVERY file on the site) and then filters by [{filt.group(1)}]. On a large site that enumeration is the slow part; navigating to the folder (SharePoint.Contents) or reading the file by URL avoids listing everything.", query=q.name))
            else:
                findings.append(_finding("info", "sharepoint_files_unfiltered", f"'{q.name}' calls SharePoint.Files with no recognizable name/folder filter.", query=q.name))

    # -- early pruning ------------------------------------------------------------
    # Judge only queries that themselves pull a list/file. A loaded table that
    # merely references a staging query which already selects columns isn't
    # "unpruned" -- the staging query is where the pull and pruning happen.
    direct_pull = re.compile(r'\{\s*\[\s*(?:Id|Name|Title|Url)\s*=\s*"[^"]+"\s*\]\s*\}\s*\[\s*(?:Items|Content|Data)\s*\]')
    fn_names = list(fn_queries)
    pruning = []
    for name, q in queries.items():
        pulls_here = (bool(direct_pull.search(q.text)) or bool(mquery.iter_calls(q.text, fn_names))
                      or any(c["function"] == "SharePoint.Files" for c in q.calls))
        if q.is_function or not pulls_here or not q.parsed.get("parsed"):
            continue
        steps = q.parsed.get("steps") or []
        first_reduce = None
        for i, st in enumerate(steps):
            m = _LEADING_FUNC.match(st.get("formula", ""))
            if i > 0 and m and m.group(1) in _REDUCERS:
                first_reduce = i
                break
        sys_removed = 0
        for st in steps:
            if st.get("formula", "").lstrip().startswith("Table.RemoveColumns"):
                cols = re.findall(r'"([^"]+)"', st["formula"])
                sys_removed += sum(1 for c in cols if c.lower() in _SP_SYSTEM_COLS)
        pruning.append({"query": name, "steps_total": len(steps), "first_reduction_step": (first_reduce + 1) if first_reduce is not None else None,
                        "system_columns_removed_by_name": sys_removed})
        if first_reduce is None and len(steps) > 1:
            findings.append(_finding("medium", "no_column_or_row_reduction", f"'{name}' loads a SharePoint source and never selects columns or filters rows: every column (including SharePoint system columns) and row is loaded.", query=name))
        elif first_reduce is not None and first_reduce >= 4:
            findings.append(_finding("info", "late_reduction", f"'{name}' first reduces rows/columns at step {first_reduce + 1}; SharePoint data can't be pruned at the source, so reducing earlier lowers the work every later step does.", query=name))
        if sys_removed >= 3:
            findings.append(_finding("info", "system_columns_removed_by_name", f"'{name}' deletes {sys_removed} SharePoint system columns by name. A Table.SelectColumns whitelist is sturdier: a new list column would otherwise flow into the model unnoticed.", query=name))

    # -- silent failures ------------------------------------------------------------
    silent = []
    for q in queries.values():
        silent.extend(silent_failures(q))
    for s in silent:
        findings.append(_finding("high", "error_swallowed", f"'{s['query']}' turns an error into an empty/null/false value ('otherwise ...'). The refresh then SUCCEEDS with missing or blank data instead of failing loudly: ...{s['snippet']}...", query=s["query"]))

    # -- navigation dependents / options ------------------------------------------------
    nav = []
    for n, q in queries.items():
        if any(c["function"] == "SharePoint.Tables" for c in q.calls):
            deps = sorted(x for x in qmodel.dependents(queries)[n])
            downstream = sorted({d for d in qmodel.dependents(queries)[n]} | {x.name for x in queries.values() if n in qmodel.upstream(queries, x.name)})
            opts = next((c["options"] for c in q.calls if c["function"] == "SharePoint.Tables"), {})
            nav.append({"query": n, "options": opts, "direct_dependents": deps, "all_downstream": downstream})
            if len(downstream) >= 3:
                findings.append(_finding("info", "shared_sharepoint_navigation", f"'{n}' (the SharePoint navigation table) sits under {len(downstream)} queries; if the site is slow or unreachable, all of them fail or slow together.", query=n))

    hard_ids = sorted({g for q in queries.values() for g in re.findall(_GUID, q.text)})
    if hard_ids:
        findings.append(_finding("info", "hardcoded_list_ids", f"{len(hard_ids)} SharePoint list/item GUID(s) are hard-coded in query text. If a list is deleted and recreated its ID changes and the load fails.", count=len(hard_ids)))

    return {
        "sharepoint_query_count": len(sp_queries),
        "list_and_file_pulls": sorted(pulls.values(), key=lambda p: -p["used_by_count"]),
        "sharepoint_files_calls": files_pulls,
        "navigation_queries": nav,
        "pruning": pruning,
        "silent_failures": silent,
        "findings": _sorted(findings),
        "summary": _summary(findings),
        "limits": "This is static analysis of M text. For what actually happened at runtime (HTTP requests, failures, engine-reported folding failures) use read_mashup_traces; Query Diagnostics does not expose SharePoint request detail.",
    }


# ============================================================================
# 5. Refresh dependencies
# ============================================================================

def refresh_dependencies(queries: dict[str, Q], is_diagnostics) -> dict[str, Any]:
    rev = qmodel.dependents(queries)
    dmemo: dict[str, int] = {}
    nodes = []
    for n, q in queries.items():
        srcs = qmodel.all_sources(queries, n)
        nodes.append({
            "query": n, "scope": q.scope, "loaded": q.loaded, "kind": "parameter" if q.is_parameter else ("function" if q.is_function else "query"),
            "depends_on": sorted(r for r in q.refs if r in queries), "depended_on_by": sorted(rev[n]),
            "depth": qmodel.depth(queries, n, dmemo), "direct_sources": sorted({c["display"] for c in q.classified}),
            "all_sources": sorted(c["display"] for c in srcs.values()),
        })

    impact: dict[str, dict[str, Any]] = {}
    for n, q in queries.items():
        for key, c in qmodel.all_sources(queries, n).items():
            e = impact.setdefault(key, {"source": c["display"], "kind": c["kind"], "loaded_tables_affected": [], "queries": []})
            e["queries"].append(n)
            if q.loaded:
                e["loaded_tables_affected"].append(n)
    impact_list = sorted(({**v, "queries": sorted(set(v["queries"])), "loaded_tables_affected": sorted(set(v["loaded_tables_affected"]))} for v in impact.values()),
                         key=lambda v: -len(v["loaded_tables_affected"]))

    findings: list[dict[str, Any]] = []
    orphans = [n for n, q in queries.items()
               if not q.loaded and not q.is_parameter and not rev[n] and not is_diagnostics(n)]
    for n in orphans:
        findings.append(_finding("info", "orphan_query", f"'{n}' isn't loaded and nothing references it: leftover staging/experiment (or only used by DAX/report). Safe to delete only after checking.", query=n))
    for n in queries:
        if len(rev[n]) >= 3 and not queries[n].is_parameter:
            findings.append(_finding("info", "high_fan_out", f"'{n}' feeds {len(rev[n])} queries; a failure here breaks all of them: {sorted(rev[n])[:8]}", query=n))
    for imp in impact_list[:5]:
        if len(imp["loaded_tables_affected"]) >= 3:
            findings.append(_finding("info", "source_single_point_of_failure", f"{imp['source']} feeds {len(imp['loaded_tables_affected'])} loaded tables; if it is unreachable they all fail to refresh.", source=imp["source"]))
    cycles = qmodel.find_cycles(queries)
    for cy in cycles:
        findings.append(_finding("high", "circular_reference", f"Circular query reference: {' -> '.join(cy)}", cycle=cy))
    params = [{"parameter": n, "value": q.param_value, "used_by": sorted(rev[n])} for n, q in queries.items() if q.is_parameter]
    return {
        "queries": sorted(nodes, key=lambda x: (-x["depth"], x["query"])),
        "source_impact": impact_list,
        "orphan_queries": orphans,
        "parameters": params,
        "max_depth": max((x["depth"] for x in nodes), default=0),
        "findings": _sorted(findings),
        "summary": _summary(findings),
    }
