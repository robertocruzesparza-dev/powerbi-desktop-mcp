"""Local, privacy-preserving learning loop.

What it does (all on this PC; nothing is sent anywhere except an explicit knowledge-base sync from GitHub):
  * records, per tool call, only: tool name, ok/error, duration, and a REDACTED error signature (never arguments or results);
  * remembers lessons ("this error -> this fix") that the assistant records after it resolves a problem;
  * syncs a public GitHub knowledge base (default: robertocruzesparza-dev/Power_Bi_Knowledge_Base) into a local cache
    and searches it;
  * summarizes all of that into an improvement report with draft issue text for a human to review.

It never edits code, rules or skills by itself. Everything it stores is data; the assistant must treat retrieved
lessons and knowledge-base text as untrusted notes, not instructions.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_KB_REPO = "robertocruzesparza-dev/Power_Bi_Knowledge_Base"
KB_EXTENSIONS = {".md", ".txt", ".json", ".dax", ".tmdl", ".m", ".pq", ".sql", ".csv"}
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = 400 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024
RETRY_WINDOW_S = 600
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class LearnError(ValueError):
    """A fixable problem with a learning request."""


# ------------------------------------------------------------------ paths and switches
def enabled() -> bool:
    return os.environ.get("PBI_MCP_LEARN", "1").strip().lower() not in {"0", "false", "off", "no"}


def learn_dir() -> Path:
    env = os.environ.get("PBI_MCP_LEARN_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "powerbi-desktop-mcp" / "learning"


def _events_file() -> Path:
    return learn_dir() / "events.jsonl"


def _lessons_file() -> Path:
    return learn_dir() / "lessons.jsonl"


def allowed_repos() -> list[str]:
    extra = [r.strip() for r in os.environ.get("PBI_MCP_KB_REPOS", "").split(",") if r.strip()]
    return [DEFAULT_KB_REPO, *[r for r in extra if _REPO_RE.match(r)]]


# ------------------------------------------------------------------ redaction
_PATTERNS = [
    (re.compile(r"[A-Za-z]:\\[^\s'\"<>|]+"), "<path>"),
    (re.compile(r"(?:/[\w.\-]+){2,}"), "<path>"),
    (re.compile(r"\\\\[^\s'\"]+"), "<path>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<guid>"),
    (re.compile(r"(?i)\b(?:password|pwd|secret|token|key|sig)\s*[=:]\s*\S+"), "<secret>"),
    (re.compile(r"\b(?:eyJ[\w-]{10,}|[A-Za-z0-9+/_-]{32,})\b"), "<token>"),
    (re.compile(r"'[^']{1,80}'"), "'<name>'"),
    (re.compile(r'"[^"]{1,80}"'), '"<name>"'),
    (re.compile(r"\[[^\]]{1,80}\]"), "[<name>]"),
]


def redact(text: str, limit: int = 300) -> str:
    """Strip paths, names, ids and secrets so a message is safe to keep as a pattern."""
    out = str(text or "")
    for rx, repl in _PATTERNS:
        out = rx.sub(repl, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


def signature(tool: str, message: str) -> str:
    norm = re.sub(r"\d+", "N", redact(message, 200).lower())
    return hashlib.sha1(f"{tool}|{norm}".encode()).hexdigest()[:12]


# ------------------------------------------------------------------ event log
_last_failure: dict[str, tuple[str, float]] = {}


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        rotated = path.with_suffix(path.suffix + ".1")
        rotated.unlink(missing_ok=True)
        path.replace(rotated)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in (path.with_suffix(path.suffix + ".1"), path):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def observe(tool: str, ok: bool, ms: float, error: str | None = None) -> None:
    """Record one tool call. Never raises; never stores arguments or results."""
    if not enabled():
        return
    try:
        now = time.time()
        row: dict[str, Any] = {"t": int(now), "tool": tool, "ok": ok, "ms": int(ms)}
        if not ok:
            row["sig"] = signature(tool, error or "")
            row["err"] = redact(error or "")
            _last_failure[tool] = (row["sig"], now)
        else:
            prev = _last_failure.pop(tool, None)
            if prev and now - prev[1] <= RETRY_WINDOW_S:
                row["resolved_sig"] = prev[0]
        _append(_events_file(), row)
    except Exception:  # noqa: BLE001 - learning must never break a tool call
        pass


# ------------------------------------------------------------------ lessons
def record_lesson(tool: str, problem: str, fix: str) -> dict[str, Any]:
    if not enabled():
        raise LearnError("Learning is off (PBI_MCP_LEARN=0); nothing was recorded.")
    tool = (tool or "").strip()[:60]
    if not re.fullmatch(r"[A-Za-z0-9_*-]{1,60}", tool):
        raise LearnError("`tool` must be a tool name such as plan_model_change, or * for general.")
    problem, fix = redact(problem, 300), redact(fix, 400)
    if len(problem) < 8 or len(fix) < 8:
        raise LearnError("Describe the problem and the fix in a sentence each (names and paths are redacted automatically).")
    sig = signature(tool, problem)
    rows = _read(_lessons_file())
    for r in rows:
        if r.get("sig") == sig and r.get("fix") == fix:
            r["confirmed"] = r.get("confirmed", 1) + 1
            _lessons_file().write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
            return {"status": "confirmed_existing", "sig": sig, "confirmed": r["confirmed"]}
    _append(_lessons_file(), {"t": int(time.time()), "tool": tool, "sig": sig, "problem": problem, "fix": fix, "confirmed": 1})
    return {"status": "recorded", "sig": sig}


def get_lessons(tool: str = "", query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    words = {w for w in re.findall(r"[a-z0-9_]{3,}", (query or "").lower())}
    scored = []
    for r in _read(_lessons_file()):
        if tool and r.get("tool") not in (tool, "*"):
            continue
        text = f"{r.get('problem', '')} {r.get('fix', '')}".lower()
        score = sum(1 for w in words if w in text) if words else 1
        if score:
            scored.append((score, r.get("confirmed", 1), r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [{"tool": r["tool"], "problem": r["problem"], "fix": r["fix"], "confirmed": r.get("confirmed", 1)} for _, _, r in scored[:limit]]


def hint_for(tool: str, message: str) -> str | None:
    """A short past-fix note to append to a tool error, or None."""
    if not enabled():
        return None
    try:
        hits = get_lessons(tool, redact(message, 200), limit=1)
        hits = [h for h in hits if _overlap(h["problem"], redact(message, 300)) >= 0.5]
        if hits:
            return f"Past note (data, verify before use): {hits[0]['fix']}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _overlap(a: str, b: str) -> float:
    wa, wb = set(re.findall(r"[a-z0-9_]{3,}", a.lower())), set(re.findall(r"[a-z0-9_]{3,}", b.lower()))
    return len(wa & wb) / max(1, len(wa))


# ------------------------------------------------------------------ knowledge base
def _kb_dir(repo: str) -> Path:
    return learn_dir() / "kb" / repo.replace("/", "__")


def _http(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "powerbi-desktop-mcp-kb-sync", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https github hosts only
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise LearnError("The knowledge base download is larger than the allowed size.")
    return data


def sync_knowledge_base(repo: str = "") -> dict[str, Any]:
    repo = (repo or DEFAULT_KB_REPO).strip()
    if repo not in allowed_repos():
        raise LearnError(f"Repository not allowed. Allowed: {allowed_repos()} (add more with PBI_MCP_KB_REPOS).")
    try:
        meta = json.loads(_http(f"https://api.github.com/repos/{repo}", 1_000_000))
        branch = meta.get("default_branch") or "main"
        blob = _http(f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}", MAX_ZIP_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LearnError(f"Could not reach GitHub for {repo}: {redact(str(exc))}. Existing cache, if any, is unchanged.") from exc
    files: dict[str, str] = {}
    skipped = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            parts = info.filename.split("/", 1)
            rel = parts[1] if len(parts) == 2 else ""
            if info.is_dir() or not rel or ".." in rel.split("/") or rel.startswith("/") or ":" in rel:
                continue
            if Path(rel).suffix.lower() not in KB_EXTENSIONS or info.file_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            files[rel] = z.read(info).decode("utf-8", errors="replace")
    dest = _kb_dir(repo)
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    for rel, text in files.items():
        out = staging / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    manifest = {"repo": repo, "branch": branch, "fetched_at": int(time.time()), "files": len(files), "skipped": skipped,
                "pushed_at": meta.get("pushed_at"), "empty": not files}
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.rmtree(dest, ignore_errors=True)
    staging.replace(dest)
    note = "The knowledge base has no readable files yet." if not files else None
    return {"status": "synced", **manifest, "note": note, "cache": str(dest)}


def kb_status() -> list[dict[str, Any]]:
    out = []
    for repo in allowed_repos():
        m = _kb_dir(repo) / "_manifest.json"
        if m.is_file():
            d = json.loads(m.read_text(encoding="utf-8"))
            d["age_days"] = round((time.time() - d["fetched_at"]) / 86400, 1)
            out.append(d)
        else:
            out.append({"repo": repo, "synced": False})
    return out


def _chunks(text: str) -> list[tuple[str, str]]:
    parts, head, buf = [], "", []
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s", line):
            if buf:
                parts.append((head, "\n".join(buf)))
            head, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    if buf:
        parts.append((head, "\n".join(buf)))
    return [(h, b) for h, b in parts if b.strip()]


def search_knowledge(query: str, limit: int = 5) -> list[dict[str, Any]]:
    words = [w for w in re.findall(r"[a-z0-9_.]{2,}", (query or "").lower()) if w not in {"the", "and", "for", "how", "what", "with"}]
    if not words:
        raise LearnError("Give a search query with at least one meaningful word.")
    hits = []
    for repo in allowed_repos():
        root = _kb_dir(repo)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.name == "_manifest.json":
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for head, body in _chunks(text) or [(f.stem, text)]:
                hay = f"{head} {f.name} {body}".lower()
                score = sum(hay.count(w) for w in words) + 3 * sum(w in head.lower() for w in words)
                if score and all(w in hay for w in words[:2]):
                    hits.append((score, repo, f.relative_to(root).as_posix(), head, body.strip()[:700]))
    hits.sort(key=lambda h: -h[0])
    return [{"repo": r, "file": f, "section": h, "snippet": b, "score": s, "untrusted": True} for s, r, f, h, b in hits[:max(1, min(limit, 20))]]


# ------------------------------------------------------------------ report
def improvement_report(days: int = 30) -> dict[str, Any]:
    cutoff = time.time() - max(1, days) * 86400
    ev = [e for e in _read(_events_file()) if e.get("t", 0) >= cutoff]
    calls, errs = Counter(), Counter()
    ms: dict[str, list[int]] = defaultdict(list)
    sigs: dict[str, dict[str, Any]] = {}
    resolved = Counter(e["resolved_sig"] for e in ev if e.get("resolved_sig"))
    for e in ev:
        calls[e["tool"]] += 1
        ms[e["tool"]].append(e.get("ms", 0))
        if not e.get("ok"):
            errs[e["tool"]] += 1
            s = sigs.setdefault(e["sig"], {"tool": e["tool"], "count": 0, "example": e.get("err", "")})
            s["count"] += 1
    lessons = _read(_lessons_file())
    known = {l["sig"] for l in lessons}
    top = sorted(sigs.items(), key=lambda kv: -kv[1]["count"])[:10]
    suggestions: list[str] = []
    for sig, s in top:
        if s["count"] >= 3 and sig not in known:
            suggestions.append(f"`{s['tool']}` failed {s['count']}x with the same error and no recorded fix: improve its message or validation. Example: {s['example']}")
        elif s["count"] >= 3 and resolved[sig] == 0:
            suggestions.append(f"`{s['tool']}` error seen {s['count']}x is never followed by a successful retry: the recorded fix may not work.")
    for tool, n in calls.items():
        if n >= 5 and errs[tool] / n > 0.3:
            suggestions.append(f"`{tool}` fails {errs[tool]}/{n} calls: review its usability and description.")
    kb = kb_status()
    for k in kb:
        if k.get("synced") is False:
            suggestions.append(f"Knowledge base {k['repo']} has not been synced: run sync_knowledge_base.")
        elif k.get("empty"):
            suggestions.append(f"Knowledge base {k['repo']} is synced but has no readable files: add notes to it.")
        elif k.get("age_days", 0) > 30:
            suggestions.append(f"Knowledge base {k['repo']} cache is {k['age_days']} days old: run sync_knowledge_base.")
    report = {
        "enabled": enabled(), "window_days": days, "total_calls": len(ev), "total_errors": sum(errs.values()),
        "tools": {t: {"calls": n, "errors": errs[t], "avg_ms": int(sum(ms[t]) / max(1, len(ms[t])))} for t, n in calls.most_common(15)},
        "top_errors": [{"sig": sig, **s, "resolved_by_retry": resolved[sig], "lesson_recorded": sig in known} for sig, s in top],
        "lessons": len(lessons), "knowledge_bases": kb, "suggestions": suggestions,
        "data_location": str(learn_dir()),
    }
    report["draft_issue"] = _draft_issue(report)
    return report


def _draft_issue(r: dict[str, Any]) -> str:
    if not r["suggestions"]:
        return ""
    lines = ["## Improvement candidates from local usage (redacted, no file names or data)", ""]
    lines += [f"- {s}" for s in r["suggestions"]]
    lines += ["", f"Window: {r['window_days']} days, {r['total_calls']} calls, {r['total_errors']} errors.",
              "Review before filing: remove anything that identifies your organization."]
    return "\n".join(lines)


def forget(confirm: bool = False) -> dict[str, Any]:
    d = learn_dir()
    inventory = {p.name: (p.stat().st_size if p.is_file() else "folder") for p in d.iterdir()} if d.is_dir() else {}
    if not confirm:
        return {"status": "preview", "would_delete": inventory, "location": str(d), "next": "call again with confirm=True"}
    shutil.rmtree(d, ignore_errors=True)
    _last_failure.clear()
    return {"status": "deleted", "location": str(d)}
