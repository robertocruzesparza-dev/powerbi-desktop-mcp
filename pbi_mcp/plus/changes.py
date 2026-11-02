"""Plan -> apply -> verify -> (auto) rollback for edits to a Power BI project.

Every write the plugin makes to project files goes through here:
  * `plan()` computes the exact before/after of each file and returns a diff. Nothing is written.
  * `apply()` re-checks the files are unchanged, backs them up outside the project, writes them
    atomically, re-validates the whole project and restores the backup if a NEW high-severity
    finding appears.
  * `restore()` puts a backup back (only if the files still match what the apply wrote).
Backups live in %LOCALAPPDATA%\\powerbi-desktop-mcp\\backups (override: PBI_MCP_BACKUP_DIR), never
inside the project, so Desktop and Git never see them.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import checks
from .project import Project, locate

PLAN_TTL = 60 * 60
DIFF_CHARS = 60000
_PLANS: dict[str, dict[str, Any]] = {}


class ChangeError(ValueError):
    pass


def backup_root() -> Path:
    raw = os.environ.get("PBI_MCP_BACKUP_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "powerbi-desktop-mcp" / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def writes_enabled() -> bool:
    return os.environ.get("PBI_MCP_READONLY", "").strip().lower() not in ("1", "true", "yes")


def _inside(child: Path, parent: Path) -> bool:
    c, p = os.path.normcase(str(child.resolve())), os.path.normcase(str(parent.resolve()))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def check_write_target(path: Path) -> None:
    """Gate for tools that create files outside the plan/apply flow (new projects, copies, documents)."""
    if not writes_enabled():
        raise ChangeError("Writes are disabled: PBI_MCP_READONLY is set. Unset it and restart the server to allow this.")
    raw = os.environ.get("PBI_MCP_EDIT_ROOT", "").strip()
    if raw and not _inside(path, Path(raw).expanduser()):
        raise ChangeError(f"Writes are restricted to {raw} (PBI_MCP_EDIT_ROOT); {path} is outside it.")


def check_edit_allowed(project: Project) -> None:
    if not writes_enabled():
        raise ChangeError("Writes are disabled: PBI_MCP_READONLY is set. Unset it and restart the server to allow edits.")
    raw = os.environ.get("PBI_MCP_EDIT_ROOT", "").strip()
    if raw and not _inside(project.root, Path(raw).expanduser()):
        raise ChangeError(f"Edits are restricted to {raw} (PBI_MCP_EDIT_ROOT). Copy the project there first (copy_project).")


def _has_link(path: Path, stop: Path) -> bool:
    """True if `path` or any parent up to `stop` is a symlink or directory junction."""
    stop_n = os.path.normcase(str(stop.resolve()))
    cur = path
    for _ in range(64):
        if cur.is_symlink() or (hasattr(cur, "is_junction") and cur.is_junction()):
            return True
        if os.path.normcase(str(cur)) == stop_n or cur.parent == cur:
            return False
        cur = cur.parent
    return True


def _prune_plans() -> None:
    now = time.time()
    for k in [k for k, v in _PLANS.items() if now - v["created"] > PLAN_TTL]:
        _PLANS.pop(k, None)


def _replace(src: Path, dst: Path, tries: int = 8) -> None:
    """os.replace with retries: on Windows, antivirus, OneDrive sync or an open Desktop can briefly lock a file."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.15 * (attempt + 1))


def _unlink(path: Path, tries: int = 8) -> None:
    for attempt in range(tries):
        try:
            path.unlink()
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.15 * (attempt + 1))


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> tuple[str | None, bool]:
    """(text, had_bom). Missing file -> (None, False)."""
    if not path.is_file():
        return None, False
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


def _project_key(project: Project) -> str:
    return f"{project.name}-{hashlib.sha1(str(project.root).lower().encode()).hexdigest()[:8]}"


def plan(project: Project, edits: dict[Path, str | None], description: str, risk: str = "") -> dict[str, Any]:
    """`edits` maps absolute path -> new text (None = delete file). Returns the plan view."""
    check_edit_allowed(project)
    if not edits:
        raise ChangeError("Nothing to change: the edit produced no differences.")
    files: dict[str, dict[str, Any]] = {}
    diffs: list[str] = []
    _prune_plans()
    for path, after in edits.items():
        if _has_link(path, project.root):
            raise ChangeError(f"{path} is, or sits under, a symbolic link or junction; refusing.")
        path = path.resolve()
        if not _inside(path, project.root):
            raise ChangeError(f"{path} is outside the project folder; refusing.")
        before, bom = _read(path)
        if after is not None and before and "\r\n" in before and "\r\n" not in after:
            after = after.replace("\n", "\r\n")                    # keep the file's own line endings
        if before == after:
            continue
        rel = path.relative_to(project.root.resolve()).as_posix()
        _rel_parts(rel)
        files[str(path)] = {"rel": rel, "before_sha": _sha(before), "after": after, "before": before, "bom": bom,
                            "action": "create" if before is None else "delete" if after is None else "modify"}
        diffs.append("".join(difflib.unified_diff(
            (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}" if after is not None else "/dev/null", n=2)))
    if not files:
        raise ChangeError("Nothing to change: the edit produced no differences.")
    baseline = checks.validate_project(project)
    impact = _preview_impact(project, files, baseline)
    pid = uuid.uuid4().hex[:12]
    _PLANS[pid] = {"id": pid, "project_root": str(project.root), "project": project.record(), "files": files, "description": description,
                   "created": time.time(), "baseline_high": [list(k) for k in checks.high_keys(baseline)]}
    diff = "\n".join(d for d in diffs if d)
    return {
        "plan_id": pid, "status": "planned", "description": description, "risk": risk or "Reopen the project in Power BI Desktop to confirm it loads; Desktop does not hot-reload external edits.",
        "files": [{"file": v["rel"], "action": v["action"]} for v in files.values()],
        "diff": diff[:DIFF_CHARS], "diff_truncated": len(diff) > DIFF_CHARS,
        "baseline_high_findings": baseline["counts"]["high"],
        "predicted_new_errors": impact,
        "expires_in_minutes": PLAN_TTL // 60,
        "next": ("Nothing was written. " + ("The diff above was TRUNCATED: list every file in `files` to the user and show each one before asking. " if len(diff) > DIFF_CHARS else "") + (
            f"WARNING: applying this would introduce {len(impact)} new error(s) and would be rolled back automatically; fix the plan first. "
            if impact else "A dry run on a temporary copy found no new errors. ")
            + "Show the user the diff, and only after they approve call apply_change(plan_id)."),
    }


def _preview_impact(project: Project, files: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the plan to a throw-away copy of the project's own folders and report blocking errors that would be NEW."""
    import tempfile
    from .project import _ignore
    base = checks.high_keys(baseline)
    root = project.root.resolve()
    with tempfile.TemporaryDirectory(prefix="pbi-preview-") as tmp:
        dest = Path(tmp) / "p"
        dest.mkdir()

        def mirror(src: Path | None) -> Path | None:
            if src is None:
                return None
            src = src.resolve()
            try:
                rel = src.relative_to(root)
            except ValueError:
                return src                                   # outside the root: read-only for validation
            target = dest / rel
            if src.is_dir():
                shutil.copytree(src, target, ignore=_ignore)
            elif src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            return target

        clone = Project(dest, project.name, mirror(project.pbip), mirror(project.model_dir), mirror(project.report_dir))
        for info in files.values():
            target = dest / info["rel"]
            if info["after"] is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(info["after"].encode("utf-8"))
        try:
            after = checks.validate_project(clone)
        except Exception as exc:  # noqa: BLE001
            return [{"severity": "high", "rule": "PRJ900", "where": "project", "message": f"Project cannot be re-read after the edit: {exc}"}]
        prefix = str(dest.resolve())
        fresh = []
        for f in after["findings"]:
            if not checks.is_blocking(f):
                continue
            norm = {**f, "where": f["where"].replace(prefix, str(project.root)), "message": f["message"].replace(prefix, str(project.root))}
            if (norm["rule"], norm["where"], norm["message"]) not in base:
                fresh.append(norm)
    return fresh[:25]


def apply(plan_id: str) -> dict[str, Any]:
    rec = _PLANS.get(plan_id or "")
    if rec is None:
        raise ChangeError("Unknown or already-applied plan_id. Plans live only in this server session; propose the change again.")
    if time.time() - rec["created"] > PLAN_TTL:
        _PLANS.pop(plan_id, None)
        raise ChangeError("This plan expired. Propose the change again.")
    project = Project.from_record(rec["project"])
    check_edit_allowed(project)
    for p, info in rec["files"].items():
        if not _inside(Path(p).resolve(), project.root) or _has_link(Path(p), project.root):
            raise ChangeError(f"{info['rel']} now resolves outside the project or through a link; nothing was applied.")
        now, _ = _read(Path(p))
        if _sha(now) != info["before_sha"]:
            _PLANS.pop(plan_id, None)
            raise ChangeError(f"{info['rel']} changed after the plan was made (Desktop save? another edit?). Nothing was applied; plan again.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = backup_root() / _project_key(project) / f"{stamp}-{plan_id}"
    (bdir / "files").mkdir(parents=True, exist_ok=True)
    manifest = {"id": plan_id, "project_root": str(project.root), "project": project.record(), "description": rec["description"], "applied": stamp, "files": []}
    for p, info in rec["files"].items():
        entry = {"path": p, "rel": info["rel"], "existed": info["before"] is not None, "after_sha": _sha(info["after"])}
        if info["before"] is not None:
            dest = bdir / "files" / info["rel"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        manifest["files"].append(entry)
    (bdir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    written: list[str] = []
    _PLANS.pop(plan_id, None)                                     # a plan is single use, whatever happens next
    try:
        for p, info in rec["files"].items():
            path = Path(p)
            if info["after"] is None:
                _unlink(path)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + f".{plan_id}.tmp")
                data = info["after"].encode("utf-8")
                if info["bom"]:
                    data = b"\xef\xbb\xbf" + data
                tmp.write_bytes(data)
                _replace(tmp, path)
            written.append(p)
        project = Project.from_record(rec["project"])
        result = checks.validate_project(project)
        new_high = [f for f in result["findings"] if checks.is_blocking(f)
                    and [f["rule"], f["where"], f["message"]] not in rec["baseline_high"]]
    except Exception as exc:  # noqa: BLE001 -- any failure must restore the originals
        failures = _restore_files(manifest, bdir)
        if failures:
            raise ChangeError(f"Write failed ({exc}) and these files could NOT be restored: {failures}. "
                              f"The untouched originals are in {bdir / 'files'}; copy them back by hand.") from exc
        raise ChangeError(f"Write failed ({exc}); the originals were restored.") from exc
    if new_high:
        failures = _restore_files(manifest, bdir)
        if failures:
            raise ChangeError(f"The edit introduced new errors and these files could NOT be restored: {failures}. "
                              f"The untouched originals are in {bdir / 'files'}; copy them back by hand.")
        return {"plan_id": plan_id, "status": "rolled_back_validation_failed", "restored": True,
                "new_high_findings": new_high[:20],
                "next": "The edit introduced new errors, so the originals were restored automatically. Fix the plan and propose again."}
    return {"plan_id": plan_id, "status": "applied", "backup_id": f"{_project_key(project)}/{stamp}-{plan_id}",
            "files_written": [f["rel"] for f in manifest["files"]], "validation": {"ok": result["ok"], "counts": result["counts"]},
            "next": "Static validation passed. Power BI Desktop does not hot-reload: reopen the project (File > Open) and confirm no repair prompt. "
                    "To undo: restore_backup(backup_id)."}


def _rel_parts(rel: str) -> list[str]:
    parts = str(rel).replace("\\", "/").split("/")
    if not rel or str(rel).startswith(("/", "\\")) or ":" in str(rel) or any(p in ("", "..") for p in parts):
        raise ChangeError(f"Backup manifest has an unsafe path {rel!r}; refusing.")
    return parts


def _entry_path(manifest: dict[str, Any], entry: dict[str, Any]) -> Path:
    """Where a backed-up file belongs: rebuilt from the project root and a validated relative path, never trusted from the manifest."""
    root = Path(manifest["project_root"]).resolve()
    target = (root / Path(*_rel_parts(entry.get("rel", "")))).resolve()
    if not _inside(target, root):
        raise ChangeError(f"Backup manifest path {entry.get('rel')!r} resolves outside the project; refusing.")
    return target


def _restore_files(manifest: dict[str, Any], bdir: Path) -> list[str]:
    """Put every backed-up file back. Never stops at the first failure; returns the files that could not be restored."""
    failures: list[str] = []
    for entry in manifest["files"]:
        try:
            path = _entry_path(manifest, entry)
            if entry["existed"]:
                src = (bdir / "files" / Path(*_rel_parts(entry["rel"]))).resolve()
                if not _inside(src, bdir):
                    raise ChangeError("backup file path escapes the backup folder")
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".restore.tmp")
                shutil.copy2(src, tmp)
                _replace(tmp, path)
            elif path.exists():
                _unlink(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{entry.get('rel')}: {exc}")
    return failures


def list_backups(project_path: str | None = None, limit: int = 30) -> dict[str, Any]:
    root = backup_root()
    dirs = [root / _project_key(locate(project_path))] if project_path else [d for d in root.iterdir() if d.is_dir()]
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for b in sorted(d.iterdir(), reverse=True):
            mf = b / "manifest.json"
            if mf.is_file():
                m = json.loads(mf.read_text(encoding="utf-8"))
                out.append({"backup_id": f"{d.name}/{b.name}", "description": m["description"], "applied": m["applied"],
                            "files": [f["rel"] for f in m["files"]], "project_root": m["project_root"]})
    out.sort(key=lambda r: r["applied"], reverse=True)
    return {"backups": out[:limit], "total": len(out)}


_SAFE_PART = re.compile(r"^[A-Za-z0-9][\w.\-]*$")


def restore(backup_id: str, confirm: bool = False) -> dict[str, Any]:
    parts = backup_id.replace("\\", "/").split("/")
    if len(parts) != 2 or not all(_SAFE_PART.match(x) and ".." not in x for x in parts):
        raise ChangeError("backup_id must look like '<project-key>/<timestamp>-<id>' as returned by apply_change or list_project_backups.")
    root = backup_root()
    bdir = (root / parts[0] / parts[1]).resolve()
    if not _inside(bdir, root):
        raise ChangeError("backup_id resolves outside the backup folder; refusing.")
    mf = bdir / "manifest.json"
    if not mf.is_file():
        raise ChangeError(f"No backup {backup_id}.")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    project = Project.from_record(manifest["project"]) if manifest.get("project") else locate(manifest["project_root"])
    check_edit_allowed(project)
    entries = [(entry, _entry_path(manifest, entry)) for entry in manifest["files"]]
    for entry, path in entries:
        now, _ = _read(path)
        if _sha(now) != entry["after_sha"]:
            raise ChangeError(f"{entry['rel']} was modified after that change was applied, so restoring would erase later work. "
                              f"Nothing was changed. The backup copy is in {bdir / 'files'}.")
    if not confirm:
        return {"status": "preview", "backup_id": backup_id, "description": manifest["description"],
                "would_restore": [e["rel"] for e, _ in entries],
                "next": "Nothing was changed. Ask the user, then call again with confirm=True."}
    failures = _restore_files(manifest, bdir)
    if failures:
        raise ChangeError(f"Some files could not be restored: {failures}. The backup copies are in {bdir / 'files'}.")
    return {"status": "restored", "backup_id": backup_id, "files": [f["rel"] for f in manifest["files"]],
            "next": "Reopen the project in Power BI Desktop (it does not hot-reload)."}
