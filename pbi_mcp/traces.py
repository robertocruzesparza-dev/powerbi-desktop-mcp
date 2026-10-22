"""Reader for Power BI Desktop's mashup-engine trace logs.

Power BI Desktop (Store build, verified on 2.157.x) writes engine traces to
    %USERPROFILE%\\Microsoft\\Power BI Desktop Store App\\Traces\\
        Microsoft.Mashup.Container.NetFX45.<pid>.<timestamp>.log
Each line is `DataMashup.Trace <Level>: <n> : {json}`. Unlike Query
Diagnostics (which leaves Data Source Query empty for SharePoint), these logs
DO record the connector-level traffic:
  * SharePoint: every HTTP request (method, URI, status, duration), retries
    and failures. Repeated RenderListDataAsStream POSTs for one list inside
    one evaluation are only a *lead* for paging -- verified NOT to be proof
    (a 4-row list showed up to 4 requests per evaluation).
  * SQL: connection opens, the CommandText actually sent, durations, and the
    number of rows read.
  * Engine-reported folding failures (HandlersQuery/ReportFoldingFailure/*)
    and container crashes.
Request bodies are not logged, so page *contents* and the SharePoint view
XML can't be seen -- only how many requests happened and how they went.
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

_LINE = re.compile(r"^DataMashup\.Trace (\w+): (\d+) : (\{.*\})\s*$")
_GUID = re.compile(r"guid'([0-9a-fA-F-]{36})'")
_DROP_KEYS = {"JwtExpires", "JwtIdentifier", "JwtTokenAudience", "ProductVersion", "HostProcessId"}


def trace_dirs() -> list[str]:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or ""
    cands = [
        os.path.join(home, "Microsoft", "Power BI Desktop Store App", "Traces"),
        os.path.join(home, "Microsoft", "Power BI Desktop", "Traces"),
        os.path.join(local, "Microsoft", "Power BI Desktop", "Traces"),
    ]
    return [c for c in cands if os.path.isdir(c)]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z?$", value)
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.strptime(m.group(1) + "." + frac, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)


def _secs(value: str | None) -> float:
    try:
        h, m, s = (value or "0:0:0").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


def read_events(since_minutes: int, max_files: int = 80, max_total_mb: int = 400) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dirs = trace_dirs()
    meta: dict[str, Any] = {"trace_dirs": dirs, "files_read": 0, "bytes_read": 0, "skipped_unparseable_lines": 0}
    if not dirs:
        return [], meta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    files = []
    for d in dirs:
        for f in glob.glob(os.path.join(d, "Microsoft.Mashup.Container.*.log")):
            try:
                st = os.stat(f)
            except OSError:
                continue
            if datetime.fromtimestamp(st.st_mtime, timezone.utc) >= cutoff:
                files.append((st.st_mtime, f, st.st_size))
    files.sort(reverse=True)
    events: list[dict[str, Any]] = []
    budget = max_total_mb * 1024 * 1024
    for _mt, f, size in files[:max_files]:
        if meta["bytes_read"] + size > budget:
            break
        meta["files_read"] += 1
        meta["bytes_read"] += size
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _LINE.match(line)
                if not m:
                    continue
                try:
                    e = json.loads(m.group(3))
                except ValueError:
                    meta["skipped_unparseable_lines"] += 1
                    continue
                ts = _parse_ts(e.get("Start"))
                if ts is None or ts < cutoff:
                    continue
                e["_level"], e["_ts"] = m.group(1), ts
                for k in _DROP_KEYS:
                    e.pop(k, None)
                events.append(e)
    return events, meta


def _endpoint(method: str | None, uri: str) -> str:
    if "RenderListDataAsStream" in uri:
        return "user_info_list" if "SiteUserInfoList" in uri else "list_data"
    if re.search(r"/_api/Web/lists\?", uri, re.I) or uri.rstrip("/").lower().endswith("/_api/web/lists"):
        return "navigation"
    if "/_api/v2.0/" in uri and "/items" in uri:
        return "items_preview"
    if re.search(r"GetFileByServerRelativ|GetFolderByServerRelativ|/Files", uri, re.I):
        return "files"
    return "other"


_ENDPOINT_HELP = {
    "navigation": "Lists the site's lists (SharePoint.Tables navigation table). Fetched again for each evaluation that touches the site.",
    "list_data": "Pulls a list's rows (RenderListDataAsStream). Several requests to one list in one evaluation can be paging OR metadata/repeat calls -- compare with the list's row count (see paging_note).",
    "items_preview": "The Power Query Editor preview endpoint (items ... $stop=200): a 200-row preview, NOT the full refresh load.",
    "user_info_list": "Resolves person/user columns via SiteUserInfoList: extra requests caused by people/lookup fields in a list.",
    "files": "File/folder access.",
    "other": "",
}


def analyze(events: list[dict[str, Any]], meta: dict[str, Any], known_lists: dict[str, list[str]] | None = None) -> dict[str, Any]:
    known_lists = {k.lower(): v for k, v in (known_lists or {}).items()}
    out: dict[str, Any] = {**meta, "events_parsed": len(events)}
    if events:
        out["window"] = {"from": min(e["_ts"] for e in events).isoformat(), "to": max(e["_ts"] for e in events).isoformat()}
    out["resource_kinds"] = dict(Counter(e.get("ResourceKind") for e in events if e.get("ResourceKind")))

    # ---------------- SharePoint ----------------
    sp = [e for e in events if e.get("Action") == "Engine/IO/Web/Request/GetResponse" and e.get("ResourceKind") == "SharePoint"]
    by_ep: dict[str, dict[str, Any]] = defaultdict(lambda: {"requests": 0, "seconds": 0.0, "max_seconds": 0.0, "failed": 0, "evaluations": set()})
    lists: dict[str, dict[str, Any]] = {}
    pages: dict[tuple[str, str], int] = Counter()
    failures: dict[tuple[str, str, str], dict[str, Any]] = {}
    slow = []
    for e in sp:
        uri = e.get("RequestUri") or ""
        ep = _endpoint(e.get("RequestMethod"), uri)
        d = _secs(e.get("Duration"))
        failed = e["_level"] == "Error" or (e.get("ResponseStatusCode") not in (None, "200", "201", "204"))
        b = by_ep[ep]
        b["requests"] += 1
        b["seconds"] += d
        b["max_seconds"] = max(b["max_seconds"], d)
        b["failed"] += int(failed)
        b["evaluations"].add(e.get("ActivityId"))
        g = _GUID.search(uri)
        gid = g.group(1).lower() if g else None
        if gid:
            L = lists.setdefault(gid, {"list_id": gid, "requests": 0, "seconds": 0.0, "failures": 0, "evaluations": set(), "endpoints": Counter()})
            L["requests"] += 1
            L["seconds"] += d
            L["failures"] += int(failed)
            L["evaluations"].add(e.get("ActivityId"))
            L["endpoints"][ep] += 1
            if ep == "list_data":
                pages[(e.get("ActivityId"), gid)] += 1
        if failed:
            status = e.get("StatusCode") or e.get("ResponseStatusCode") or "error"
            key = (gid or ep, ep, str(status))
            f = failures.setdefault(key, {"list_id": gid, "endpoint": ep, "status": str(status), "description": e.get("StatusDescription"), "count": 0,
                                          "first": e["_ts"].isoformat(), "last": e["_ts"].isoformat()})
            f["count"] += 1
            f["last"] = max(f["last"], e["_ts"].isoformat())
        slow.append((d, ep, gid, e["_ts"].isoformat(), e.get("RequestMethod")))
    ppe = defaultdict(list)
    for (_act, gid), n in pages.items():
        ppe[gid].append(n)
    list_rows = []
    for gid, L in lists.items():
        p = ppe.get(gid, [])
        list_rows.append({
            "list_id": gid, "in_open_model": gid in known_lists, "queries": known_lists.get(gid, []),
            "requests": L["requests"], "evaluations": len(L["evaluations"]), "failed_requests": L["failures"],
            "total_seconds": round(L["seconds"], 2),
            "max_list_data_requests_in_one_evaluation": max(p) if p else None,
            "evaluations_with_multiple_requests": sum(1 for n in p if n > 1),
        })
    list_rows.sort(key=lambda r: (-r["failed_requests"], -r["total_seconds"]))
    nav = by_ep.get("navigation")
    out["sharepoint"] = {
        "requests": len(sp),
        "by_endpoint": [
            {"endpoint": ep, "requests": b["requests"], "evaluations": len(b["evaluations"]), "total_seconds": round(b["seconds"], 2),
             "max_seconds": round(b["max_seconds"], 2), "failed": b["failed"], "meaning": _ENDPOINT_HELP.get(ep, "")}
            for ep, b in sorted(by_ep.items(), key=lambda kv: -kv[1]["requests"])
        ],
        "navigation_fetches": nav["requests"] if nav else 0,
        "navigation_evaluations": len(nav["evaluations"]) if nav else 0,
        "lists": list_rows,
        "failures": sorted(failures.values(), key=lambda f: -f["count"]),
        "slowest_requests": [{"seconds": round(s, 2), "endpoint": ep, "list_id": g, "at": at, "method": m}
                             for s, ep, g, at, m in sorted(slow, reverse=True)[:8]],
        "paging_note": "More than one list_data request per evaluation is NOT proof of paging: on the test tenant a list with only 4 rows in the model still showed up to 4 requests in one evaluation. Request bodies (view XML, row limit) and response row counts are not logged, so page size and page count cannot be read from these logs. Treat repeated requests as a lead only when the list is large (compare rows_in_model against requests).",
    }
    retries = [e for e in events if e.get("Action") == "Engine/IO/Web/Request/RunWithRetry/Failure"]
    out["sharepoint"]["retry_events"] = len(retries)

    # ---------------- SQL ----------------
    opens = [e for e in events if e.get("Action") == "Engine/IO/Db/Microsoft SQL/Connection/Open"]
    cmds = [e for e in events if e.get("Action") == "Engine/IO/Db/Microsoft SQL/Command/ExecuteDbDataReader"]
    readers = defaultdict(list)
    for e in events:
        if e.get("Action") == "Engine/IO/Db/Microsoft SQL/DbDataReader":
            readers[e.get("ConnectionId")].append(e)
    sql_rows = []
    for c in cmds:
        text = c.get("CommandText") or ""
        if re.match(r"\s*SELECT\s+@@version", text, re.I):
            continue  # connector's own capability probe
        rc = None
        for r in sorted(readers.get(c.get("ConnectionId"), []), key=lambda r: r["_ts"]):
            if r["_ts"] >= c["_ts"] and r.get("RowCount") not in (None, "-1"):
                rc = int(r["RowCount"])
                break
        sql_rows.append({"at": c["_ts"].isoformat(), "server": c.get("Server"), "database": c.get("Database"),
                         "seconds_to_first_result": round(_secs(c.get("Duration")), 2), "rows_read": rc,
                         "command_timeout_s": c.get("CommandTimeout"), "characters": len(text),
                         "command_preview": " ".join(text.split())[:240]})
    nq = [_secs(e.get("Duration")) for e in events if e.get("Action") == "Engine/IO/Db/Microsoft SQL/NativeQueryTableValue/Execute"]
    out["sql"] = {
        "connections_opened": len(opens),
        "slowest_connection_open_seconds": round(max((_secs(e.get("Duration")) for e in opens), default=0), 2),
        "commands": sorted(sql_rows, key=lambda r: -(r["rows_read"] or 0)),
        "total_rows_read": sum(r["rows_read"] or 0 for r in sql_rows),
        "native_query_executions": {"count": len(nq), "max_seconds": round(max(nq, default=0), 2), "total_seconds": round(sum(nq), 2)},
        "cancelled_commands": sum(1 for e in events if e.get("Action") == "Engine/IO/Db/Microsoft SQL/Command/Cancel"),
    }

    # ---------------- Engine-reported folding failures ----------------
    ff = [e for e in events if "/ReportFoldingFailure/" in (e.get("Action") or "")]
    kinds_by_activity: dict[str, set[str]] = defaultdict(set)
    lists_by_activity: dict[str, set[str]] = defaultdict(set)
    for e in events:
        a = e.get("ActivityId")
        if a and e.get("ResourceKind"):
            kinds_by_activity[a].add(e["ResourceKind"])
        g = _GUID.search(e.get("RequestUri") or "")
        if a and g:
            lists_by_activity[a].add(g.group(1).lower())
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in ff:
        m = re.search(r"Message: (.*?)(?:\r?\n|StackTrace)", e.get("Exception") or "", re.S)
        msg = (m.group(1).strip() if m else "")[:160]
        op = e["Action"].rsplit("/", 1)[-1]
        g = groups.setdefault((op, e["_level"], msg), {"operation": op, "level": e["_level"], "message": msg, "count": 0, "sources": Counter(), "list_ids": set()})
        g["count"] += 1
        for k in kinds_by_activity.get(e.get("ActivityId"), ()):
            g["sources"][k] += 1
        g["list_ids"] |= lists_by_activity.get(e.get("ActivityId"), set())
    out["folding_failures"] = {
        "events": len(ff),
        "groups": [{"operation": g["operation"], "level": g["level"], "message": g["message"], "count": g["count"],
                    "sources_seen_in_same_evaluation": dict(g["sources"]),
                    "list_ids": sorted(g["list_ids"])[:6]} for g in sorted(groups.values(), key=lambda g: -g["count"])],
        "meaning": "The engine itself reporting that it could not push a SelectColumns/SelectRows down to the source. 'sources_seen_in_same_evaluation' shows which connector(s) that evaluation was talking to.",
    }

    # ---------------- Other errors / crashes ----------------
    crashes = []
    for e in events:
        if "ExceptionHandler" in (e.get("Action") or ""):
            m = re.search(r"Message: (.*?)(?:\r?\n|StackTrace)", e.get("Exception") or "", re.S)
            crashes.append({"at": e["_ts"].isoformat(), "message": (m.group(1).strip() if m else "")[:220]})
    other_err = Counter(e.get("Action") for e in events if e["_level"] == "Error"
                        and "ReportFoldingFailure" not in (e.get("Action") or "") and e.get("ResourceKind") != "SharePoint"
                        and "ExceptionHandler" not in (e.get("Action") or ""))
    out["container_crashes"] = {"count": len(crashes), "samples": crashes[:5],
                                "note": "A FileLoadException/'Could not load file or assembly' here usually means Power BI Desktop was updated while running (mixed old/new binaries): close all instances and reopen."}
    out["other_errors"] = dict(other_err.most_common(10))
    return out
