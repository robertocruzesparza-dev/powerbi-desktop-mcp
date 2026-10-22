"""Plain-text "snapshot" of a Power BI dashboard, built to be read by a no-code Microsoft 365 Copilot agent.

Why this exists: a Copilot agent built in Agent Builder can't call local tools, but it can read files in
SharePoint/OneDrive. So the analysis the Claude plugin does live is run once on the user's PC and written out
as small .txt files (the only text format Agent Builder accepts besides Office/PDF; Markdown and JSON are not
supported). Microsoft also advises keeping each file short (about 36,000 characters) and avoiding tables, so:
  * output is plain `key: value` lines, never tables;
  * each section is split into parts of at most `max_chars`;
  * every part starts with the dashboard name and snapshot time (agents can't answer by file name);
  * nothing reads table rows -- only structure, code, settings and counts. (Values someone typed into the
    dashboard itself, such as filter values or DAX constants, do appear.) Obvious credentials found in M/SQL
    text are redacted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

HEADER_PREFIX = "POWER BI SNAPSHOT"
DEFAULT_MAX_CHARS = 30_000
_HEADER_RESERVE = 400
_MAX_LINE = 4_000

_SECRET = re.compile(
    r"(?i)\b(password|pwd|passwd|client[_-]?secret|api[_-]?key|account[_-]?key|access[_-]?token|shared[_-]?access[_-]?signature)"
    r"(\s*[\"']?\s*[=:]\s*[\"']?)([^\"';,\s)]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{20,}")


# --------------------------------------------------------------------------
# Rendering: any JSON-like value -> readable lines
# --------------------------------------------------------------------------

def _scalar(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple, set))


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_ALWAYS_NOISE = {"LineageTag", "SourceLineageTag", "ShowAsVariationOnly", "IsPrivate"}


def _noise(key: str, value: Any) -> bool:
    """Engine bookkeeping that tells a reader nothing and costs the agent context: lineage tags and the
    model GUID repeated on every row."""
    return key in _ALWAYS_NOISE or (key == "Model" and isinstance(value, str) and bool(_GUID.match(value)))


def _inline_row(item: dict) -> str | None:
    """One-line form of a flat dict, or None if it has nested or multi-line values."""
    parts = []
    for key, value in item.items():
        if _empty(value) or _noise(key, value):
            continue
        if isinstance(value, (list, tuple)) and all(_is_scalar(v) for v in value):
            text = ", ".join(_scalar(v) for v in value)  # e.g. the fields a visual uses
        elif _is_scalar(value):
            text = _scalar(value)
        else:
            return None
        if "\n" in text:
            return None
        parts.append(f"{key}={text}")
    return " | ".join(parts) if parts else "(empty)"


def to_lines(value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if _noise(key, item):
                continue
            if _is_scalar(item):
                text = _scalar(item)
                if "\n" in text:  # code (DAX / M / SQL): keep verbatim, indented
                    lines.append(f"{pad}{key}:")
                    lines += [f"{pad}  {ln}" for ln in text.split("\n")]
                else:
                    lines.append(f"{pad}{key}: {text}")
            elif isinstance(item, dict):
                lines.append(f"{pad}{key}: (none)" if not item else f"{pad}{key}:")
                lines += to_lines(item, indent + 2)
            else:
                seq = list(item)
                if not seq:
                    lines.append(f"{pad}{key}: (none)")
                elif all(_is_scalar(s) for s in seq):
                    lines.append(f"{pad}{key}: " + ", ".join(_scalar(s) for s in seq))
                else:
                    lines.append(f"{pad}{key} ({len(seq)} items):")
                    lines += to_lines(seq, indent + 2)
        return lines
    if isinstance(value, (list, tuple, set)):
        lines = []
        for item in value:
            if isinstance(item, dict):
                inline = _inline_row(item)
                if inline is not None:
                    lines.append(f"{pad}- {inline}")
                else:
                    nested = to_lines(item, indent + 2)
                    lines.append(f"{pad}- " + (nested[0].lstrip() if nested else "(empty)"))
                    lines += nested[1:]
            elif isinstance(item, (list, tuple, set)):
                lines.append(f"{pad}-")
                lines += to_lines(item, indent + 2)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines
    return [f"{pad}{_scalar(value)}"]


def redact(text: str) -> tuple[str, int]:
    """Blank out obvious credentials (Password=..., api_key: ..., Bearer ...). Returns (text, count)."""
    count = 0

    def sub_secret(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED]"

    def sub_bearer(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}[REDACTED]"

    return _BEARER.sub(sub_bearer, _SECRET.sub(sub_secret, text)), count


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Group lines into parts of at most `limit` characters, breaking at line boundaries (and preferably before
    a new '== subsection ==' once a part is more than 60% full). Over-long lines are split."""
    pieces: list[str] = []
    for line in lines:
        while len(line) > min(limit, _MAX_LINE):
            cut = min(limit, _MAX_LINE)
            pieces.append(line[:cut])
            line = "    (continued) " + line[cut:]
        pieces.append(line)

    parts: list[list[str]] = [[]]
    size = 0
    for line in pieces:
        add = len(line) + 1
        full = size + add > limit
        new_block = line.startswith("== ") and size > 0.6 * limit
        if parts[-1] and (full or new_block):
            parts.append([])
            size = 0
        parts[-1].append(line)
        size += add
    return ["\n".join(p) for p in parts if p] or [""]


# --------------------------------------------------------------------------
# Building a snapshot
# --------------------------------------------------------------------------

Producer = Callable[[], Any]


@dataclass
class Section:
    num: str
    slug: str
    title: str
    items: list[tuple[str, Producer]] = field(default_factory=list)  # (label, call)
    skipped: str = ""  # why this section was not captured at all


@dataclass
class SectionResult:
    section: Section
    status: str  # OK | PARTIAL | FAILED | NOT CAPTURED
    detail: list[str]
    files: list[Path]


@dataclass
class SnapshotResult:
    out_dir: Path
    sections: list[SectionResult]
    redactions: int
    files: list[Path]

    @property
    def failed(self) -> list[SectionResult]:
        return [s for s in self.sections if s.status in ("FAILED", "PARTIAL")]


MAX_PATH_BUDGET = 250  # Windows (and OneDrive sync) choke at about 260 characters per full path
_LONGEST_FILENAME = len("02_model_structure_part10of10.txt") + 6


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")[:60].strip(" .")
    return cleaned or "dashboard"


def check_path_budget(out_dir: Path) -> None:
    """Fail early, in plain words, if the files would not fit under Windows' path-length limit."""
    worst = len(str(out_dir.resolve())) + 1 + _LONGEST_FILENAME
    if worst > MAX_PATH_BUDGET:
        raise ValueError(
            f"The output folder path is too long for Windows ({worst} characters; the limit is about 260): {out_dir}\n"
            "Choose a shorter folder with --out (for example C:\\PBI-Snapshots), then move or sync it into SharePoint.")


def remove_old_snapshot_files(out_dir: Path) -> int:
    """Delete .txt files this tool wrote earlier (identified by their first line) so a re-export never leaves
    stale parts behind. Anything else in the folder is left alone."""
    removed = 0
    for path in out_dir.glob("*.txt"):
        try:
            with path.open(encoding="utf-8") as f:
                first = f.readline()
        except (OSError, UnicodeDecodeError):
            continue
        if first.startswith(HEADER_PREFIX):
            path.unlink()
            removed += 1
    return removed


def _header(name: str, taken: str, section: str, part: int, parts: int) -> str:
    return f"{HEADER_PREFIX} | Dashboard: {name} | Taken: {taken} | Section: {section} | Part {part} of {parts}"


def _write_parts(out_dir: Path, name: str, taken: str, sec: Section, body_lines: list[str], limit: int) -> list[Path]:
    parts = chunk_lines(body_lines, max(1_000, limit - _HEADER_RESERVE))
    files = []
    for i, body in enumerate(parts, 1):
        suffix = f"_part{i}of{len(parts)}" if len(parts) > 1 else ""
        path = out_dir / f"{sec.num}_{sec.slug}{suffix}.txt"
        header = _header(name, taken, f"{sec.num} {sec.title}", i, len(parts))
        path.write_text(f"{header}\n\n{body}\n", encoding="utf-8")
        files.append(path)
    return files


def build_snapshot(name: str, out_dir: Path, sections: list[Section], *, taken: datetime, source_path: str | None = None,
                   max_chars: int = DEFAULT_MAX_CHARS, tool_version: str = "", progress: Callable[[str], None] = lambda s: None) -> SnapshotResult:
    check_path_budget(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    remove_old_snapshot_files(out_dir)
    stamp = taken.strftime("%Y-%m-%d %H:%M")
    redactions = 0
    results: list[SectionResult] = []

    for sec in sections:
        if sec.skipped:
            results.append(SectionResult(sec, "NOT CAPTURED", [sec.skipped], []))
            progress(f"  [skip] {sec.num} {sec.title}: {sec.skipped}")
            continue
        body: list[str] = []
        problems: list[str] = []
        ok = 0
        for label, call in sec.items:
            progress(f"  [....] {sec.num} {sec.title} / {label}")
            body += ["", f"== {label} =="]
            try:
                body += to_lines(call())
                ok += 1
            except Exception as exc:  # noqa: BLE001 -- one failing check must never hide the rest
                msg = f"{type(exc).__name__}: {str(exc)[:300]}"
                problems.append(f"{label}: {msg}")
                body.append(f"CHECK FAILED - this part is MISSING from the snapshot. {msg}")
        status = "FAILED" if problems and not ok else "PARTIAL" if problems else "OK"
        text, n = redact("\n".join(body))
        redactions += n
        files = _write_parts(out_dir, name, stamp, sec, text.split("\n"), max_chars)
        results.append(SectionResult(sec, status, problems, files))
        progress(f"  [{status.lower():>7}] {sec.num} {sec.title}: {len(files)} file(s)")

    readme = _write_readme(out_dir, name, stamp, source_path, results, redactions, tool_version, max_chars)
    all_files = [readme] + [f for r in results for f in r.files]
    return SnapshotResult(out_dir, results, redactions, all_files)


def _write_readme(out_dir: Path, name: str, stamp: str, source_path: str | None, results: list[SectionResult],
                  redactions: int, tool_version: str, limit: int) -> Path:
    lines = [
        f"Dashboard: {name}",
        f"Snapshot taken: {stamp}  (a point-in-time copy; the real dashboard may have changed since)",
        f"Source file: {source_path or 'unknown'}",
        f"Exporter version: {tool_version or 'unknown'}",
        "",
        "WHAT THIS IS",
        "A read-only, plain-text export of the dashboard's structure and settings: model, DAX, Power Query (M),",
        "connections, row-level security, report layout and automated checks. It contains NO table rows. It does",
        "include any values typed into the dashboard itself (filter values, constants in DAX or M code, names in",
        "security filters). Obvious credentials in code were replaced with [REDACTED]"
        + (f" ({redactions} replaced)." if redactions else " (none found)."),
        "It cannot show live data, run DAX, or see anything changed after the snapshot time.",
        "",
        "SECTIONS (status of each; read the files of every section before concluding)",
    ]
    for r in results:
        files = ", ".join(f.name for f in r.files) or "no files"
        lines.append(f"- {r.section.num} {r.section.title}: {r.status} ({files})")
        for d in r.detail:
            lines.append(f"    {'reason' if r.status == 'NOT CAPTURED' else 'problem'}: {d}")
    bad = [r for r in results if r.status != "OK"]
    lines += ["", "ANYTHING NOT OK ABOVE MEANS THAT PART IS MISSING OR INCOMPLETE. Say so to the user instead of guessing."
              if bad else "All sections were captured.",
              "", "NOT CAPTURED BY DESIGN: row-level data values, table profiles, anything not in the sections above,",
              "and the contents of other dashboards (each dashboard has its own folder)."]
    body = "\n".join(lines)
    header = _header(name, stamp, "00 README", 1, 1)
    path = out_dir / "00_README.txt"
    path.write_text(f"{header}\n\n{body}\n", encoding="utf-8")
    assert len(body) < limit  # the README is small by construction
    return path


# --------------------------------------------------------------------------
# What goes in each section
# --------------------------------------------------------------------------

def default_sections(instance: str | None, pbix_path: str | None, *, quality: bool = True, traces: bool = False) -> list[Section]:
    """The standard sections. `instance` None means Power BI Desktop isn't open: only the report layout (which
    is read from the .pbix file) can be captured."""
    from pbi_mcp import server as s

    live = instance is not None
    why_model = "" if live else "Power BI Desktop was not open with this file (the model can only be read while it is open)."
    why_path = "" if pbix_path else "the .pbix file path is not known"
    inst = instance or ""

    audit_items = [("audit", lambda: s.audit_dashboard(inst, pbix_path, quality, traces))]
    quality_items = [("relationship integrity", lambda: s.check_relationship_integrity(inst))] if quality else []
    if pbix_path:
        quality_items.append(("unused fields", lambda: s.find_unused_fields(inst, pbix_path)))
    layout_items = [("report layout", lambda: s.get_report_layout(pbix_path))]
    conn_items = [
        ("connections", lambda: s.classify_connections(inst)),
        ("data sources", lambda: s.list_data_sources(inst)),
        ("embedded SQL", lambda: s.analyze_sql_queries(inst)),
        ("query folding", lambda: s.analyze_folding_risk(inst, False)),
        ("SharePoint patterns", lambda: s.analyze_sharepoint_patterns(inst, True)),
        ("refresh dependencies", lambda: s.check_refresh_dependencies(inst)),
    ]
    if traces:
        conn_items.append(("runtime logs", lambda: s.read_mashup_traces(inst, 1440)))

    return [
        Section("01", "audit", "Audit findings", audit_items, why_model),
        Section("02", "model_structure", "Model structure", [
            ("tables", lambda: s.list_tables(inst)), ("columns", lambda: s.list_columns(inst, None)),
            ("relationships", lambda: s.analyze_relationships(inst)), ("hierarchies", lambda: s.list_hierarchies(inst)),
            ("size", lambda: s.analyze_model_size(inst, 25))], why_model),
        Section("03", "dax", "DAX measures and calculations", [("DAX", lambda: s.list_all_dax_expressions(inst))], why_model),
        Section("04", "power_query", "Power Query", [
            ("refresh health", lambda: s.diagnose_power_query(inst)),
            ("queries (M code)", lambda: s.list_power_query(inst, None, False))], why_model),
        Section("05", "connections", "Connections, SQL, folding, SharePoint", conn_items, why_model),
        Section("06", "security", "Row-level security", [("roles", lambda: s.list_security_roles(inst))], why_model),
        Section("07", "report_layout", "Report layout, visuals and filters", layout_items, why_path),
        Section("08", "data_quality", "Data quality and unused fields", quality_items,
                why_model or ("" if quality_items else "data-quality checks were turned off")),
    ]
