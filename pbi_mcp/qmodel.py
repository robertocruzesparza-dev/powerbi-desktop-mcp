"""Query model: every Power Query (M) query in the file as a node in a
dependency graph, with its data-source calls parsed and classified.

This is the shared foundation for the connection, SQL, folding, SharePoint
and refresh-dependency analyzers. It works purely on M *source text* (what
list_power_query returns), so everything here is static analysis: it can say
what a query is wired to and how, but it cannot observe runtime behavior
(row counts at a step, whether a filter actually folded, HTTP traffic).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from . import mquery

# Wrapper parsers whose real source is the call nested inside them
# (Excel.Workbook(File.Contents(...)) reads a *file*, not "Excel").
_PARSER_WRAPPERS = {"Excel.Workbook"}
_SOURCE_FUNCS = tuple(sorted(set(mquery.DATA_SOURCE_FUNCTIONS)))

_CLOUD_SQL_SUFFIXES = (
    ".database.windows.net", ".database.azure.com", ".sql.azuresynapse.net",
    ".database.fabric.microsoft.com", ".datawarehouse.fabric.microsoft.com",
    ".database.usgovcloudapi.net", ".database.chinacloudapi.cn", ".dfs.core.windows.net",
)
_NONPROD_WORDS = {"dev", "test", "tst", "qa", "uat", "stage", "staging", "sandbox", "demo", "old", "backup", "bak", "copy", "temp", "tmp"}
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass
class Q:
    name: str
    scope: str            # "table" (loaded into the model) | "shared" (Enable Load off / function / parameter)
    text: str
    loaded: bool
    parsed: dict[str, Any]
    is_function: bool = False
    function_params: list[str] = field(default_factory=list)
    is_parameter: bool = False
    param_value: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)      # parsed data-source calls in THIS query
    classified: list[dict[str, Any]] = field(default_factory=list) # classify_call() of each
    native: list[dict[str, Any]] = field(default_factory=list)     # embedded SQL
    refs: set[str] = field(default_factory=set)                    # other queries this one references


# ----------------------------------------------------------------------------
# Building the model
# ----------------------------------------------------------------------------

def _param_names(params_text: str | None) -> list[str]:
    if not params_text:
        return []
    out = []
    for piece in mquery.split_call_args("(" + params_text + ")"):
        m = re.match(r"\s*(?:optional\s+)?([A-Za-z_][\w]*)", piece)
        if m:
            out.append(m.group(1))
    return out


def _arg(raw: str, params: dict[str, str]) -> dict[str, Any]:
    lit = mquery.string_literal_value(raw)
    if lit is not None:
        return {"kind": "literal", "value": lit, "raw": raw}
    if re.fullmatch(r"[A-Za-z_][\w.]*", raw) and raw in params:
        return {"kind": "parameter", "param": raw, "value": params[raw], "raw": raw}
    if raw.startswith("["):
        return {"kind": "options", "value": None, "raw": raw}
    return {"kind": "expression", "value": None, "raw": raw}


def parse_call(call: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    args = [_arg(a, params) for a in mquery.split_call_args(call["args_text"])]
    options: dict[str, str] = {}
    for a in args:
        if a["kind"] == "options":
            options.update(mquery.parse_record_options(a["raw"]))
    positional = [a for a in args if a["kind"] != "options"]
    identity = [a["value"] if a["value"] is not None else "(expression) " + " ".join(a["raw"].split())[:120] for a in positional[:2]]
    return {
        "function": call["function"],
        "args": args,
        "options": options,
        "identity": identity,
        "unresolved": [a["raw"] for a in positional[:2] if a["value"] is None],
    }


def build_queries(pq: dict[str, Any], exclude=None) -> tuple[dict[str, Q], dict[str, str]]:
    """Turn list_power_query(include_steps=True) output into a Q graph.
    `exclude(name) -> bool` drops queries from the graph entirely (used to
    hide Query Diagnostics output, which reads a temp trace file and would
    otherwise raise false "local path" alarms)."""
    raw: list[tuple[str, str, str, bool, dict[str, Any]]] = []
    for t in pq.get("table_queries", []):
        if t.get("is_m") is False:
            continue  # DAX calculated table, not an M query
        if exclude and exclude(t["table"]):
            continue
        raw.append((t["table"], "table", t["m_query"] or "", True, t.get("m_steps") or mquery.parse_m(t["m_query"] or "")))
    for s in pq.get("shared_queries_and_parameters", []):
        if exclude and exclude(s["name"]):
            continue
        raw.append((s["name"], "shared", s["m_query"] or "", False, s.get("m_steps") or mquery.parse_m(s["m_query"] or "")))

    params: dict[str, str] = {}
    for name, _scope, text, _loaded, _parsed in raw:
        p = mquery.parse_parameter(text)
        if p is not None:
            params[name] = p["value"]

    queries: dict[str, Q] = {}
    for name, scope, text, loaded, parsed in raw:
        q = Q(name=name, scope=scope, text=text, loaded=loaded, parsed=parsed)
        q.is_function = bool(parsed.get("is_function"))
        q.function_params = _param_names(parsed.get("parameters"))
        q.is_parameter = name in params
        q.param_value = params.get(name)
        queries[name] = q

    names = list(queries)
    for q in queries.values():
        step_names = {s.get("name") for s in (q.parsed.get("steps") or []) if s.get("name")}
        ignore = step_names | set(q.function_params) | {q.name}
        q.refs = mquery.find_query_refs(q.text, names, ignore) - {q.name}
        calls = [parse_call(c, params) for c in mquery.iter_calls(q.text, _SOURCE_FUNCS)]
        real = {c["function"] for c in calls}
        # drop wrapper parsers when the file/URL they read is itself a source call
        if real - _PARSER_WRAPPERS:
            calls = [c for c in calls if c["function"] not in _PARSER_WRAPPERS]
        q.calls = calls
        q.classified = [classify_call(c) for c in calls]
        q.native = mquery.find_native_queries(q.text)
    return queries, params


# ----------------------------------------------------------------------------
# Classifying a data-source call
# ----------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def classify_call(call: dict[str, Any]) -> dict[str, Any]:
    f, ident, opts = call["function"], call["identity"], call["options"]
    info: dict[str, Any] = {"function": f, "flags": [], "identity": ident}

    def flag(sev: str, code: str, msg: str) -> None:
        info["flags"].append({"severity": sev, "code": code, "message": msg})

    if call["unresolved"]:
        flag("info", "unresolved_expression",
             "A connection argument is computed by an expression (not a literal or a parameter with a literal value), so the real target can't be determined statically.")

    first = ident[0] if ident else ""
    second = ident[1] if len(ident) > 1 else ""

    if f in ("Sql.Database", "Sql.Databases"):
        server, db = first, second
        lower = server.lower()
        info.update(kind="sql_server", server=server, database=db, key=f"sql:{lower}/{db.lower()}",
                    display=f"{server} / {db}" if db else server)
        cloud = lower.endswith(_CLOUD_SQL_SUFFIXES)
        info["gateway"] = not cloud and not call["unresolved"]
        if info["gateway"]:
            flag("info", "gateway_required", "On-premises SQL Server: scheduled refresh in the Power BI service requires an on-premises data gateway that can reach this server.")
        if re.match(r"^(localhost|\(local\)|\.|127\.0\.0\.1)(\\|,|:|$)", lower):
            flag("high", "localhost_server", "Server is localhost/this machine: the model only refreshes on this PC, never in the service or on another machine.")
        if _IPV4.match(lower.split("\\")[0].split(",")[0]):
            flag("info", "ip_address_server", "Server is addressed by IP rather than name; an IP change silently breaks refresh.")
        toks = _tokens(server) | _tokens(db)
        hit = sorted(toks & _NONPROD_WORDS)
        if hit:
            flag("medium", "non_production_name", f"Server/database name contains {hit}: confirm this isn't a dev/test/backup copy feeding a production dashboard.")
        if "Query" in opts:
            flag("info", "native_query", "Uses a hand-written SQL statement ([Query=...]): query folding is disabled for every later step.")
    elif f in ("Oracle.Database", "PostgreSQL.Database", "MySQL.Database", "Teradata.Database", "SapHana.Database", "Snowflake.Databases",
               "AnalysisServices.Database", "AnalysisServices.Databases"):
        kind = "analysis_services" if f.startswith("AnalysisServices") else "database"
        info.update(kind=kind, server=first, database=second, key=f"{f.split('.')[0].lower()}:{first.lower()}/{second.lower()}",
                    display=f"{first} / {second}" if second else first, gateway=None)
    elif f in ("SharePoint.Files", "SharePoint.Tables", "SharePoint.Contents"):
        _classify_sharepoint(info, f, first, opts, flag)
    elif f in ("Web.Contents", "Web.Page", "Web.BrowserContents", "OData.Feed"):
        _classify_web(info, f, call, first, opts, flag)
    elif f in ("File.Contents", "Folder.Files", "Folder.Contents"):
        _classify_file(info, f, first, flag)
    elif f == "Excel.CurrentWorkbook":
        info.update(kind="embedded_workbook", key="embedded:workbook", display="Embedded workbook data", gateway=False)
    elif f in ("Odbc.DataSource", "Odbc.Query", "OleDb.DataSource", "OleDb.Query"):
        info.update(kind="odbc", key=f"odbc:{first.lower()}", display=first[:120], gateway=True)
        flag("info", "gateway_required", "ODBC/OLE DB sources generally require an on-premises data gateway (or a driver installed where refresh runs).")
    else:
        info.update(kind="other", key=f"{f}:{'|'.join(ident).lower()}", display=" / ".join(ident)[:120], gateway=None)
    return info


def _classify_sharepoint(info: dict[str, Any], f: str, url_text: str, opts: dict[str, str], flag) -> None:
    parts = urlsplit(url_text) if url_text.lower().startswith(("http://", "https://")) else None
    if parts is None:
        info.update(kind="sharepoint_site", key=f"sharepoint:{url_text.lower()}", display=url_text[:160], gateway=False, site=url_text)
        flag("info", "sharepoint_url_unresolved", "SharePoint URL isn't a literal, so the site can't be determined statically.")
        return
    host = parts.netloc.lower()
    path = unquote(parts.path)
    segs = [s for s in path.split("/") if s]
    site_path = "/" + "/".join(segs[:2]) if len(segs) >= 2 and segs[0].lower() in ("sites", "teams", "personal") else ""
    personal = host.endswith("-my.sharepoint.com") or (segs[:1] and segs[0].lower() == "personal")
    site = f"{host}{site_path}".lower()
    info.update(kind="onedrive_personal" if personal else "sharepoint_site", site=site,
                key=f"{'onedrive' if personal else 'sharepoint'}:{site}", display=site, gateway=False,
                url_variants=[url_text])
    if parts.scheme.lower() == "http":
        flag("high", "insecure_http", "SharePoint URL uses http:// rather than https://.")
    deeper = "/" + "/".join(segs) != site_path and len(segs) > (2 if site_path else 0)
    if deeper or ".aspx" in path.lower():
        flag("medium", "library_path_in_site_url",
             "URL points below the site (library/folder/page). SharePoint.Files/Tables/Contents expect the site URL; a deeper path can return a different scope than intended.")
    if personal:
        flag("medium", "personal_onedrive_source",
             "Source is a personal OneDrive for Business location: refresh depends on that one person's account and access, and breaks if they leave or the file moves.")
    if f == "SharePoint.Tables" and "Implementation" not in opts:
        flag("info", "sharepoint_tables_implementation_unset", "SharePoint.Tables has no Implementation option; confirm which connector implementation is intended (\"2.0\" is the current one).")
    if f in ("SharePoint.Files", "SharePoint.Contents") and "ApiVersion" not in opts:
        flag("info", "sharepoint_apiversion_unset", "No ApiVersion option set on the SharePoint connector (Microsoft's guidance is to set ApiVersion = 15).")


def _classify_web(info: dict[str, Any], f: str, call: dict[str, Any], url_text: str, opts: dict[str, str], flag) -> None:
    first_arg = call["args"][0] if call["args"] else {"raw": "", "kind": "expression"}
    dynamic = first_arg["kind"] == "expression"
    parts = urlsplit(url_text) if url_text.lower().startswith(("http://", "https://")) else None
    host = parts.netloc.lower() if parts else ""
    if host.endswith("sharepoint.com"):
        segs = [s for s in unquote(parts.path).split("/") if s]
        personal = host.endswith("-my.sharepoint.com") or (segs[:1] and segs[0].lower() == "personal")
        site_path = "/" + "/".join(segs[:2]) if len(segs) >= 2 and segs[0].lower() in ("sites", "teams", "personal") else ""
        info.update(kind="onedrive_web_file" if personal else "sharepoint_web_file", site=f"{host}{site_path}",
                    key=f"{'onedrive' if personal else 'sharepoint'}:{host}{site_path}", display=url_text[:160], gateway=False,
                    url_variants=[url_text])
        if personal:
            flag("medium", "personal_onedrive_source",
                 "Reads a file from a personal OneDrive for Business URL: refresh depends on that one person's account and access.")
    elif host:
        info.update(kind="web", host=host, key=f"web:{host}", display=url_text[:160], gateway=None)
        if parts.scheme.lower() == "http":
            flag("medium", "insecure_http", "Web source uses http:// rather than https://.")
    else:
        info.update(kind="web", key=f"web:{url_text.lower()[:80]}", display=url_text[:160] or "(dynamic URL)", gateway=None)
    if f == "Web.Contents" and dynamic and "RelativePath" not in opts:
        flag("medium", "dynamic_url",
             "The URL is built by an expression without RelativePath/Query options; the Power BI service treats this as a dynamic data source and can refuse to refresh it.")


def _classify_file(info: dict[str, Any], f: str, path: str, flag) -> None:
    low = path.lower()
    if low.startswith("\\\\") or low.startswith("//"):
        info.update(kind="unc_file", key=f"file:{low}", display=path, gateway=True)
        flag("medium", "unc_path", "UNC network path: needs a data gateway (or network access) wherever refresh runs, and the account must have share permission.")
    else:
        info.update(kind="local_file", key=f"file:{low}", display=path, gateway=True)
        if "onedrive" in low:
            flag("high", "onedrive_synced_path",
                 "Path is inside a locally *synced* OneDrive/SharePoint folder (C:\\Users\\<you>\\OneDrive...): it exists only on this Windows profile. Use the file's SharePoint/OneDrive URL (SharePoint.Files / Web.Contents) so it refreshes anywhere.")
        elif re.match(r"^[a-z]:\\", low):
            flag("high", "local_path", "Local drive path: refresh only works on a machine with this exact path, and never in the Power BI service without a gateway.")


# ----------------------------------------------------------------------------
# Graph helpers
# ----------------------------------------------------------------------------

def dependents(queries: dict[str, Q]) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = {n: set() for n in queries}
    for q in queries.values():
        for r in q.refs:
            if r in rev:
                rev[r].add(q.name)
    return rev


def all_sources(queries: dict[str, Q], name: str, _seen: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every distinct data source (by key) a query ultimately reads, following
    references to other queries/functions. {key: classified call}."""
    seen = _seen if _seen is not None else set()
    if name in seen or name not in queries:
        return {}
    seen.add(name)
    q = queries[name]
    out: dict[str, dict[str, Any]] = {c["key"]: c for c in q.classified}
    for r in q.refs:
        for k, v in all_sources(queries, r, seen).items():
            out.setdefault(k, v)
    return out


def upstream(queries: dict[str, Q], name: str, _seen: set[str] | None = None) -> set[str]:
    seen = _seen if _seen is not None else set()
    for r in queries[name].refs if name in queries else ():
        if r not in seen:
            seen.add(r)
            upstream(queries, r, seen)
    return seen


def depth(queries: dict[str, Q], name: str, _memo: dict[str, int] | None = None, _stack: tuple[str, ...] = ()) -> int:
    memo = _memo if _memo is not None else {}
    if name in memo:
        return memo[name]
    if name in _stack or name not in queries:
        return 0
    refs = [r for r in queries[name].refs if r in queries]
    d = 0 if not refs else 1 + max(depth(queries, r, memo, _stack + (name,)) for r in refs)
    memo[name] = d
    return d


def find_cycles(queries: dict[str, Q]) -> list[list[str]]:
    cycles: list[list[str]] = []
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for r in queries[n].refs:
            if r not in queries:
                continue
            if state.get(r) == 1:
                cycles.append(path[path.index(r):] + [r] if r in path else [n, r])
            elif r not in state:
                visit(r, path + [r])
        state[n] = 2

    for n in queries:
        if n not in state:
            visit(n, [n])
    return cycles
