"""Read-only review of the supported file types, and a listing of what can be edited."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import container, textedit
from .formats import CONTAINER_KINDS, classify, member_is_editable
from .guard import CHANGES_DIR, edit_root, is_in_edit_root, resolve_review_target

MAX_LISTED = 200


def review(path: str, member: str = "", offset: int = 0, max_chars: int = 6000) -> dict[str, Any]:
    target = resolve_review_target(path)
    kind = classify(target)
    member = (member or "").strip()
    head: dict[str, Any] = {"file": target.name, "kind": kind, "bytes": target.stat().st_size, "editable_here": is_in_edit_root(target)}
    if kind in CONTAINER_KINDS:
        if not member:
            parts = container.list_members(target)
            return {**head, "parts": [{**p, "text_editable": member_is_editable(kind, p["name"])} for p in parts[:MAX_LISTED]],
                    "parts_total": len(parts),
                    "note": "Only parts with text_editable=true can be read or edited. DataModel and DataMashup are compressed binary and are never read or changed here."}
        if not member_is_editable(kind, member):
            raise ValueError(f"{member!r} is not a readable text part. Call review_file without member to list parts.")
        raw = container.read_member(target, member)
    else:
        if member:
            raise ValueError("member only applies to .pbix/.pbit files.")
        raw = target.read_bytes()
    text, encoding = textedit.decode(raw)
    try:
        textedit.validate(kind, member, text)
        well_formed: Any = True
    except ValueError as exc:
        well_formed = str(exc)
    shown = textedit.redact(text)
    start = max(0, min(offset, len(shown)))
    chunk = shown[start:start + max(500, min(max_chars, 12000))]
    return {**head, "member": member, "encoding": encoding, "well_formed": well_formed, "total_chars": len(shown),
            "offset": start, "returned_chars": len(chunk), "truncated": start + len(chunk) < len(shown),
            "redactions_possible": "Text that looks like a password/token is shown as [REDACTED]; quote the real text only from the file, never edit using redacted text.",
            "text": chunk}


def find_editable_files(max_results: int = 100) -> list[dict[str, Any]]:
    root = edit_root()
    found: list[dict[str, Any]] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != CHANGES_DIR]
        for name in files:
            full = os.path.join(folder, name)
            try:
                kind = classify(Path(full))
            except ValueError:
                continue
            found.append({"path": full, "kind": kind, "bytes": os.path.getsize(full)})
            if len(found) >= max_results:
                return found
    return found
