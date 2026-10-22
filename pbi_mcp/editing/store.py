"""Propose / apply / rollback with an out-of-band approval code.

The approval code is printed in the server's console window on the PC and is never
returned to the model, so text the model read (or was tricked by) cannot approve a change.
Every apply takes a full-file backup first and re-verifies afterwards."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import container, textedit
from .formats import CONTAINER_KINDS, RISK_NOTES, classify, member_is_editable
from .guard import CHANGES_DIR, edit_root, resolve_edit_target

PROPOSAL_TTL = 30 * 60
CODE_TTL = 10 * 60
MAX_ATTEMPTS = 3
DIFF_PREVIEW_CHARS = 6000
_ID = re.compile(r"^[0-9a-f]{12}$")


def announce(message: str) -> None:
    """Show text to the person at the PC. Tests replace this to capture the code."""
    print(f"[pbi-edit] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _store() -> Path:
    directory = edit_root() / CHANGES_DIR
    (directory / "pending").mkdir(parents=True, exist_ok=True)
    return directory


def _load(change_id: str) -> dict[str, Any]:
    if not _ID.match(change_id or ""):
        raise ValueError("change_id is not valid.")
    path = _store() / "pending" / f"{change_id}.json"
    if not path.is_file():
        raise ValueError(f"No change with id {change_id}. Call propose_change first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(rec: dict[str, Any]) -> None:
    path = _store() / "pending" / f"{rec['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _journal(event: str, rec: dict[str, Any]) -> None:
    """Audit trail: ids, hashes and file names only. No file content."""
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "id": rec["id"], "kind": rec["kind"],
        "file": rec["file"], "member": rec["member"], "diff_sha": rec["diff_sha"],
        "sha_before": rec["file_sha_before"], "sha_after": rec.get("file_sha_after"),
    }
    with open(_store() / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _replace_from(source: Path, target: Path) -> None:
    """Copy `source` over `target` atomically (temp file in the target's folder)."""
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Approval codes
# --------------------------------------------------------------------------

def _hash_code(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}{code}".encode()).hexdigest()


def _issue_code(rec: dict[str, Any], purpose: str) -> dict[str, Any]:
    code = f"{secrets.randbelow(10 ** 6):06d}"
    salt = secrets.token_hex(8)
    announce(f"APPROVAL CODE to {purpose} change {rec['id']} ({rec['file']}): {code}   (valid {CODE_TTL // 60} min, 3 tries)")
    return {**rec, "code_salt": salt, "code_hash": _hash_code(salt, code), "code_expires": time.time() + CODE_TTL,
            "code_purpose": purpose, "attempts": 0}


def _check_code(rec: dict[str, Any], code: str, purpose: str) -> dict[str, Any]:
    if rec.get("code_purpose") != purpose or not rec.get("code_hash"):
        raise ValueError("No approval code is active for this action. Call again without approval_code to get a new one.")
    if rec.get("status") == "locked" or rec.get("attempts", 0) >= MAX_ATTEMPTS:
        raise ValueError("Too many wrong codes. This change is locked; call propose_change again.")
    if time.time() > rec["code_expires"]:
        raise ValueError("The approval code expired. Call again without approval_code to get a new one.")
    if hmac.compare_digest(_hash_code(rec["code_salt"], code.strip()), rec["code_hash"]):
        return {**rec, "code_hash": None}
    attempts = rec.get("attempts", 0) + 1
    updated = {**rec, "attempts": attempts, "status": "locked" if attempts >= MAX_ATTEMPTS else rec["status"]}
    _save(updated)
    raise ValueError(f"Wrong approval code ({MAX_ATTEMPTS - attempts} tries left).")


# --------------------------------------------------------------------------
# Edit planning
# --------------------------------------------------------------------------

def _read_part(target: Path, kind: str, member: str) -> tuple[str, str]:
    if kind in CONTAINER_KINDS:
        return textedit.decode(container.read_member(target, member))
    if target.stat().st_size > textedit.MAX_BYTES:
        raise ValueError("File is larger than the editing limit.")
    return textedit.decode(target.read_bytes())


def _plan(target: Path, kind: str, member: str, old: str, new: str, count: int) -> tuple[str, str, str, str]:
    before, enc = _read_part(target, kind, member)
    after = textedit.apply_replacement(before, old, new, count)
    textedit.validate(kind, member, after)
    label = f"{target.name}:{member}" if member else target.name
    return before, after, enc, textedit.make_diff(before, after, label)


def _view(rec: dict[str, Any], diff: str) -> dict[str, Any]:
    lines = diff.splitlines()
    return {
        "change_id": rec["id"], "status": rec["status"], "file": rec["file"], "member": rec["member"],
        "kind": rec["kind"], "reason": rec["reason"], "replacements": rec["expected_count"],
        "lines_added": sum(1 for l in lines if l.startswith("+") and not l.startswith("+++")),
        "lines_removed": sum(1 for l in lines if l.startswith("-") and not l.startswith("---")),
        "risk": RISK_NOTES[rec["kind"]],
        "diff": diff[:DIFF_PREVIEW_CHARS], "diff_truncated": len(diff) > DIFF_PREVIEW_CHARS,
        "expires_in_minutes": max(0, int((rec["expires"] - time.time()) // 60)),
        "next": (
            "Nothing has been changed yet. Show the user the diff exactly as given and ask whether to apply it. "
            "A 6-digit approval code was printed in the server window on the PC (it is NOT in this chat). "
            "Only if the user approves AND reads you the code, call finish_change(change_id, action='apply', approval_code=...)."
        ),
    }


def propose(path: str, old_text: str, new_text: str, reason: str, member: str = "", expected_count: int = 1) -> dict[str, Any]:
    target = resolve_edit_target(path)
    kind = classify(target)
    member = (member or "").strip()
    if kind in CONTAINER_KINDS:
        if not member:
            raise ValueError("member is required for .pbix/.pbit: call review_file on the file to list its parts.")
        if not member_is_editable(kind, member):
            raise ValueError(f"{member!r} cannot be edited. Only report-layer text parts (and DataModelSchema in a .pbit) can; the compressed model and Power Query parts are never touched.")
    elif member:
        raise ValueError("member only applies to .pbix/.pbit files.")
    if not (reason or "").strip():
        raise ValueError("reason is required: state the problem this change fixes.")
    _, _, _, diff = _plan(target, kind, member, old_text, new_text, expected_count)
    now = time.time()
    rec = {
        "id": uuid.uuid4().hex[:12], "status": "pending", "created": now, "expires": now + PROPOSAL_TTL,
        "kind": kind, "path": str(target), "file": str(target.relative_to(edit_root())), "member": member,
        "old_text": old_text, "new_text": new_text, "expected_count": expected_count, "reason": reason.strip(),
        "diff_sha": hashlib.sha256(diff.encode("utf-8")).hexdigest(), "file_sha_before": _sha_file(target),
    }
    rec = _issue_code(rec, "apply")
    _save(rec)
    _journal("proposed", rec)
    return _view(rec, diff)


# --------------------------------------------------------------------------
# Apply / rollback
# --------------------------------------------------------------------------

def finish(change_id: str, action: str, approval_code: str = "") -> dict[str, Any]:
    rec = _load(change_id)
    if action == "apply":
        return _apply(rec, approval_code)
    if action == "rollback":
        return _rollback(rec, approval_code)
    raise ValueError("action must be 'apply' or 'rollback'.")


def _code_issued(rec: dict[str, Any], purpose: str) -> dict[str, Any]:
    rec = _issue_code(rec, purpose)
    _save(rec)
    return {"change_id": rec["id"], "status": "code_issued",
            "message": f"A new approval code to {purpose} this change was printed in the server window on the PC. Ask the user to read it to you, then call again with approval_code."}


def _apply(rec: dict[str, Any], code: str) -> dict[str, Any]:
    if rec["status"] != "pending":
        raise ValueError(f"This change is {rec['status']}, not pending. Call propose_change for a new one.")
    if time.time() > rec["expires"]:
        _save({**rec, "status": "expired"})
        raise ValueError("This proposal expired. Call propose_change again.")
    if not (code or "").strip():
        return _code_issued(rec, "apply")
    rec = _check_code(rec, code, "apply")
    target = resolve_edit_target(rec["path"])
    kind, member = rec["kind"], rec["member"]
    if _sha_file(target) != rec["file_sha_before"]:
        _save({**rec, "status": "stale"})
        raise ValueError("The file changed after this was proposed. Nothing was applied; propose the change again.")
    _, after, enc, diff = _plan(target, kind, member, rec["old_text"], rec["new_text"], rec["expected_count"])
    if hashlib.sha256(diff.encode("utf-8")).hexdigest() != rec["diff_sha"]:
        raise ValueError("The diff no longer matches what was proposed. Nothing was applied.")

    backup = _store() / "backups" / rec["id"] / target.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    crc_before = container.checksums(target) if kind in CONTAINER_KINDS else {}
    tmp = target.with_name(f"{target.name}.{rec['id']}.tmp")
    try:
        data = textedit.encode(after, enc)
        if kind in CONTAINER_KINDS:
            container.rewrite_member(target, member, data, tmp)
        else:
            tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    checks = _verify(target, kind, member, after, crc_before)
    if not all(c["passed"] for c in checks):
        _replace_from(backup, target)
        failed = {**rec, "status": "failed_verify_restored"}
        _save(failed)
        _journal("failed_verify_restored", failed)
        raise ValueError(f"Verification failed after writing, so the original was restored. Failed checks: {[c for c in checks if not c['passed']]}")

    done = {**rec, "status": "applied", "applied": time.time(), "file_sha_after": _sha_file(target), "backup": str(backup)}
    _save(done)
    _journal("applied", done)
    return {
        "change_id": rec["id"], "status": "applied", "file": rec["file"], "backup_taken": True, "verify": checks,
        "next": ("Static checks passed. Live checks need Power BI Desktop: ask the user to reopen the file there, then run audit_dashboard. "
                 "To undo, call finish_change(action='rollback'); that needs a new approval code."),
    }


def _verify(target: Path, kind: str, member: str, expected_after: str, crc_before: dict[str, int]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        text, _ = _read_part(target, kind, member)
        checks.append({"check": "file re-read equals the approved edit", "passed": text == expected_after})
        textedit.validate(kind, member, text)
        checks.append({"check": "result is well-formed", "passed": True})
        if kind in CONTAINER_KINDS:
            crc_after = container.checksums(target)
            others = {k: v for k, v in crc_before.items() if k != member}
            checks.append({"check": "zip container intact", "passed": True})
            checks.append({"check": "every other part is byte-identical", "passed": all(crc_after.get(k) == v for k, v in others.items()) and set(crc_after) == set(crc_before)})
    except ValueError as exc:
        checks.append({"check": "post-write validation", "passed": False, "detail": str(exc)})
    return checks


def _rollback(rec: dict[str, Any], code: str) -> dict[str, Any]:
    if rec["status"] != "applied":
        raise ValueError(f"Only an applied change can be rolled back (this one is {rec['status']}).")
    if not (code or "").strip():
        return _code_issued(rec, "rollback")
    rec = _check_code(rec, code, "rollback")
    target = resolve_edit_target(rec["path"])
    if _sha_file(target) != rec["file_sha_after"]:
        raise ValueError(f"The file changed after this edit, so rolling back would erase later work. Nothing was changed. The backup is at {rec['backup']}.")
    _replace_from(Path(rec["backup"]), target)
    if _sha_file(target) != rec["file_sha_before"]:
        raise ValueError("Rollback wrote the backup but the file does not match the original hash. Check the file by hand; backup path: " + rec["backup"])
    done = {**rec, "status": "rolled_back", "file_sha_after": _sha_file(target)}
    _save(done)
    _journal("rolled_back", done)
    return {"change_id": rec["id"], "status": "rolled_back", "restored_original": True,
            "next": "The file is byte-identical to before the change. Tell the user, and reopen it in Power BI Desktop if it was open."}
