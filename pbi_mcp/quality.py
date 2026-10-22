"""Data-quality probes run against the loaded model with DAX.

These answer "is what got loaded complete and sane?" -- the model-side half of
"does it match the source". Comparing against the source system itself would
need credentials to that system and is deliberately not attempted; instead
`profile_table(expected_row_count=...)` lets a caller supply the source's
count (e.g. from SQL Server) and flags any mismatch.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import metadata
from .discovery import PbiInstance
from .engine import run_dax

_NUMERIC = ("integer", "int64", "double", "decimal", "currency", "number", "whole")
_DATE = ("datetime", "date", "time")
_TRUNCATION_LENGTHS = {30, 50, 100, 255, 256, 4000}   # CAST default, common limits, Excel/SharePoint text cap
_SENTINEL_YEARS = (1899, 1900, 1901, 2999, 9999)


def _col(table: str, name: str) -> str:
    return f"{metadata._quote_table(table)}[{name.replace(']', ']]')}]"


def _kind(dtype: str | None) -> str:
    d = (dtype or "").lower()
    if d.startswith(_DATE):
        return "date"
    if d.startswith(_NUMERIC):
        return "numeric"
    if d.startswith(("string", "text")):
        return "string"
    return "other"


def _val(row: dict[str, Any], key: str) -> Any:
    for k, v in row.items():
        if k == key or k.endswith(f"[{key}]"):
            return v
    return None


def profile_table(
    instance: PbiInstance,
    table: str,
    columns: list[str] | None = None,
    key_columns: list[str] | None = None,
    expected_row_count: int | None = None,
    max_columns: int = 80,
) -> dict[str, Any]:
    cols_meta = [c for c in metadata.list_columns(instance, table)["rows"]
                 if c.get("Table") == table and not str(c.get("Name", "")).startswith("RowNumber-")]
    if columns:
        wanted = set(columns)
        cols_meta = [c for c in cols_meta if c.get("Name") in wanted]
    truncated = len(cols_meta) > max_columns
    cols_meta = cols_meta[:max_columns]

    T = metadata._quote_table(table)
    total = metadata.table_row_count(instance, table)
    findings: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []

    def query_batch(batch: list[dict[str, Any]]) -> dict[str, Any]:
        parts = []
        for i, c in enumerate(batch):
            ref = _col(table, c["Name"])
            k = _kind(c.get("DataType"))
            parts.append(f'"b{i}", COUNTBLANK({ref})')
            parts.append(f'"d{i}", DISTINCTCOUNT({ref})')
            if k in ("numeric", "date"):
                parts.append(f'"mn{i}", MIN({ref})')
                parts.append(f'"mx{i}", MAX({ref})')
            elif k == "string":
                parts.append(f'"ml{i}", MAXX({T}, LEN({ref}))')
        rows = run_dax(instance, "EVALUATE ROW(" + ", ".join(parts) + ")", max_rows=1)
        return rows[0] if rows else {}

    for start in range(0, len(cols_meta), 15):
        batch = cols_meta[start:start + 15]
        per: list[tuple[dict[str, Any], dict[str, Any], int]] = []   # (column meta, result row, index within that query)
        try:
            res = query_batch(batch)
            per = [(c, res, idx) for idx, c in enumerate(batch)]
        except Exception:  # noqa: BLE001 -- isolate the column that breaks the batch
            for c in batch:
                try:
                    per.append((c, query_batch([c]), 0))
                except Exception as exc:  # noqa: BLE001
                    profiles.append({"column": c["Name"], "data_type": c.get("DataType"), "error": str(exc).splitlines()[0][:140]})
        for c, res, i in per:
            blank, distinct = _val(res, f"b{i}"), _val(res, f"d{i}")
            mn, mx, ml = _val(res, f"mn{i}"), _val(res, f"mx{i}"), _val(res, f"ml{i}")
            k = _kind(c.get("DataType"))
            p = {"column": c["Name"], "data_type": c.get("DataType"), "blank_count": blank, "distinct_count": distinct}
            if total:
                p["blank_pct"] = round(100.0 * (blank or 0) / total, 2)
            if mn is not None or mx is not None:
                p["min"], p["max"] = str(mn), str(mx)
            if ml is not None:
                p["max_text_length"] = ml
            profiles.append(p)
            name = c["Name"]
            if total and blank == total:
                findings.append({"severity": "medium", "code": "column_entirely_blank", "column": name,
                                 "message": f"'{table}'[{name}] is blank in all {total:,} rows: dead weight, or a source column that stopped arriving."})
            elif total and (blank or 0) / total >= 0.95:
                findings.append({"severity": "info", "code": "column_mostly_blank", "column": name,
                                 "message": f"'{table}'[{name}] is {p['blank_pct']}% blank."})
            elif total > 1 and distinct == 1:
                findings.append({"severity": "info", "code": "column_constant", "column": name,
                                 "message": f"'{table}'[{name}] holds a single value in every row."})
            if k == "string" and ml in _TRUNCATION_LENGTHS:
                findings.append({"severity": "medium", "code": "text_length_at_limit", "column": name,
                                 "message": f"Longest value in '{table}'[{name}] is exactly {ml} characters, a common truncation limit (30 = SQL CAST default, 255 = Excel/SharePoint text cap). Longer source values may have been cut off."})
            if k == "date" and mn is not None:
                yrs = []
                for v in (mn, mx):
                    m = re.match(r"(\d{4})", str(v))
                    if m:
                        yrs.append(int(m.group(1)))
                if any(y in _SENTINEL_YEARS or y < 1900 for y in yrs):
                    findings.append({"severity": "medium", "code": "sentinel_dates", "column": name,
                                     "message": f"'{table}'[{name}] spans {mn} to {mx}: placeholder dates (1900-01-01 / 9999-12-31) usually mean missing values encoded as dates and skew date charts/ranges."})
                elif yrs and yrs[-1] > datetime.now().year + 10:
                    findings.append({"severity": "info", "code": "far_future_dates", "column": name, "message": f"'{table}'[{name}] max date is {mx}."})

    result: dict[str, Any] = {"table": table, "row_count": total, "columns": profiles, "columns_truncated": truncated}
    if key_columns:
        refs = ", ".join(_col(table, k) for k in key_columns)
        try:
            dup = run_dax(instance, f'EVALUATE ROW("dups", COUNTROWS({T}) - COUNTROWS(SUMMARIZE({T}, {refs})), '
                                    f'"blank_keys", COUNTROWS(FILTER({T}, ' + " || ".join(f"ISBLANK({_col(table, k)})" for k in key_columns) + ")))",
                          max_rows=1)[0]
            d, b = _val(dup, "dups"), _val(dup, "blank_keys")
            result["key_check"] = {"key_columns": key_columns, "duplicate_rows": d, "rows_with_blank_key": b}
            if d:
                findings.append({"severity": "high", "code": "duplicate_keys",
                                 "message": f"{d:,} row(s) in '{table}' repeat an existing {key_columns} value: a 'unique' key isn't, which double-counts sums and breaks one-to-many relationships."})
            if b:
                findings.append({"severity": "medium", "code": "blank_keys", "message": f"{b:,} row(s) in '{table}' have a blank in {key_columns}."})
        except Exception as exc:  # noqa: BLE001
            result["key_check"] = {"key_columns": key_columns, "error": str(exc)[:160]}
    if expected_row_count is not None:
        result["expected_row_count"] = expected_row_count
        if expected_row_count != total:
            findings.append({"severity": "high", "code": "row_count_mismatch",
                             "message": f"'{table}' has {total:,} rows but {expected_row_count:,} were expected ({total - expected_row_count:+,}): rows were dropped, duplicated, or the refresh is stale."})
    order = {"high": 0, "medium": 1, "info": 2}
    result["findings"] = sorted(findings, key=lambda f: order[f["severity"]])
    result["summary"] = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "info")}
    return result


def check_relationship_integrity(instance: PbiInstance, max_relationships: int = 60) -> dict[str, Any]:
    """For every relationship, count key values that exist on the 'many' side
    but not on the 'one' side. Those rows silently disappear from anything
    filtered through the relationship or pile up under (Blank) -- a classic
    way totals stop matching with no error anywhere."""
    rels = metadata.list_relationships(instance)["rows"]
    out, findings = [], []
    checked = 0
    for r in rels:
        ft, fc, tt, tc = r.get("FromTable"), r.get("FromColumn"), r.get("ToTable"), r.get("ToColumn")
        if not all((ft, fc, tt, tc)) or str(tt).startswith(("LocalDateTable_", "DateTableTemplate_")) or str(ft).startswith(("LocalDateTable_", "DateTableTemplate_")):
            continue
        if metadata._is_diagnostics_name(ft) or metadata._is_diagnostics_name(tt):
            continue
        if checked >= max_relationships:
            break
        checked += 1
        fcard, tcard = r.get("FromCardinality"), r.get("ToCardinality")
        lookup = (fcard, tcard) in (("Many", "One"), ("One", "Many"))
        pairs = []
        if fcard == "Many" and tcard == "One":
            pairs = [((ft, fc), (tt, tc))]
        elif fcard == "One" and tcard == "Many":
            pairs = [((tt, tc), (ft, fc))]
        else:
            pairs = [((ft, fc), (tt, tc)), ((tt, tc), (ft, fc))]   # 1:1 or many:many -- check both ways
        peer_rows = []
        for (mt, mc), (ot, oc) in pairs:
            M, O = _col(mt, mc), _col(ot, oc)
            MT = metadata._quote_table(mt)
            q = (f'EVALUATE ROW("rows", COUNTROWS({MT}), '
                 f'"blank_keys", COUNTROWS(FILTER({MT}, ISBLANK({M}))), '
                 f'"orphan_keys", COUNTROWS(EXCEPT(FILTER(DISTINCT({M}), NOT ISBLANK({M})), DISTINCT({O}))), '
                 f'"orphan_rows", COUNTROWS(FILTER({MT}, NOT ISBLANK({M}) && NOT ({M} IN DISTINCT({O})))))')
            row = {"relationship": f"{mt}[{mc}] -> {ot}[{oc}]", "active": r.get("IsActive"), "checked_side": f"{mt}[{mc}]",
                   "relationship_type": "lookup" if lookup else "peer"}
            try:
                res = run_dax(instance, q, max_rows=1)[0]
                rows_, bk, ok, orow = (_val(res, k) or 0 for k in ("rows", "blank_keys", "orphan_keys", "orphan_rows"))
                pct = round(100.0 * orow / rows_, 2) if rows_ else 0.0
                row.update(rows=rows_, blank_keys=bk, orphan_keys=ok, orphan_rows=orow, orphan_row_pct=pct)
                if lookup:
                    if orow:
                        # many -> one: a fact row whose key is missing from the lookup table.
                        findings.append({"severity": "high" if pct >= 5 else "medium", "code": "orphan_keys", "relationship": row["relationship"],
                                         "message": f"{orow:,} row(s) ({pct}%) in '{mt}' have {mc} values ({ok:,} distinct) that don't exist in {ot}[{oc}]. They show under (Blank) when grouped by {ot} and are dropped from measures filtered through it."})
                else:
                    peer_rows.append((mt, mc, ot, oc, orow, rows_, pct))
                if bk:
                    findings.append({"severity": "info", "code": "blank_foreign_keys", "relationship": row["relationship"],
                                     "message": f"{bk:,} row(s) in '{mt}' have a blank {mc}, so they can't join to {ot} at all."})
            except Exception as exc:  # noqa: BLE001
                row["error"] = str(exc).splitlines()[0][:160]
            out.append(row)
        # one:one / many:many: partial overlap is normal (a 4-row table never matches most of a 127k-row one).
        # Only flag when the relationship is unmatched from BOTH sides -- the signature of wrong columns,
        # a data-type/format mismatch, or an empty side.
        if len(peer_rows) == 2 and min(x[6] for x in peer_rows) >= 50:
            lo = min(x[6] for x in peer_rows)
            (m1, c1, o1, k1, r1, n1, p1), (m2, c2, o2, k2, r2, n2, p2) = peer_rows
            findings.append({"severity": "high" if lo >= 95 else "medium",
                             "code": "relationship_barely_matches" if lo >= 95 else "relationship_mostly_unmatched",
                             "relationship": f"{m1}[{c1}] <-> {o1}[{k1}]",
                             "message": f"Unmatched from both sides: {r1:,} of {n1:,} rows ({p1}%) in '{m1}' and {r2:,} of {n2:,} rows ({p2}%) in '{m2}' have keys with no partner. Some unmatched keys are normal for a {fcard}:{tcard} relationship, but this little overlap usually means the wrong columns, a data-type/format mismatch (e.g. '2425' vs 2425, padded vs unpadded IDs), or an empty side."})
    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {
        "relationships_checked": checked,
        "checks": out,
        "findings": findings,
        "summary": {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "info")},
        "note": "Auto-generated date-table relationships are skipped. Many-to-one (lookup) relationships: any unmatched key is a real defect. One-to-one and many-to-many (peer) relationships are checked in both directions and only flagged when half or more of the keys are unmatched, since partial overlap is normal there.",
    }
