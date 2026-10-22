"""Exact-match text replacement with a validity check on the result."""
from __future__ import annotations

import difflib
import json
import re
import xml.etree.ElementTree as ET

from .formats import CONTAINER_KINDS, JSON_KINDS

MAX_BYTES = 50 * 1024 * 1024

_SECRET = re.compile(
    r"(password|pwd|secret|accountkey|sharedaccesssignature)\s*=\s*[^;\s\"'<]+"
    r"|bearer\s+[A-Za-z0-9._~+/=-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}",
    re.I,
)


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)


def decode(data: bytes) -> tuple[str, str]:
    """(text, encoding) so the file can be written back in the encoding it came in."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"File part is larger than {MAX_BYTES // (1024 * 1024)} MB; not edited.")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    # BOM-less UTF-16 (Report/Layout): ASCII text has a NUL beside every character, and UTF-8 would
    # happily decode that as text full of NULs, so look for the pattern first.
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        return data.decode("utf-16-le"), "utf-16-le"
    if len(data) >= 4 and data[0] == 0 and data[2] == 0 and data[1] != 0:
        return data.decode("utf-16-be"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    raise ValueError("The file part is not text in a known encoding (utf-8, utf-16); not edited.")


def encode(text: str, encoding: str) -> bytes:
    if encoding == "utf-16":  # a BOM was present on read; keep it
        return b"\xff\xfe" + text.encode("utf-16-le")
    return text.encode(encoding)


def apply_replacement(text: str, old: str, new: str, expected_count: int) -> str:
    if not old:
        raise ValueError("old_text must not be empty.")
    if old == new:
        raise ValueError("old_text and new_text are identical: nothing to change.")
    if not 1 <= expected_count <= 20:
        raise ValueError("expected_count must be between 1 and 20.")
    found = text.count(old)
    if found != expected_count:
        raise ValueError(f"old_text occurs {found} time(s) but expected_count is {expected_count}. Quote more surrounding text so it matches exactly the places you mean.")
    if _SECRET.search(new) and not _SECRET.search(old):
        raise ValueError("new_text looks like it contains a credential or token. Credentials are never written into files; use the platform's connection manager.")
    return text.replace(old, new)


def validate(kind: str, member: str, text: str) -> None:
    """Static check only. Power BI Desktop opening the file is the real test."""
    if "\x00" in text:
        raise ValueError("Result contains NUL characters.")
    if kind == "rdl":
        if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
            raise ValueError("DOCTYPE/ENTITY declarations are not allowed in an .rdl edit.")
        try:
            ET.fromstring(text)
        except ET.ParseError as exc:
            raise ValueError(f"Result is not well-formed XML: {exc}") from exc
    elif kind == "tmdl":
        _validate_tmdl(text)
    elif kind in JSON_KINDS or kind == "pbir" or (kind in CONTAINER_KINDS and _is_json_member(member)):
        try:
            json.loads(text)
        except ValueError as exc:
            raise ValueError(f"Result is not valid JSON: {exc}") from exc


def _is_json_member(member: str) -> bool:
    return member.endswith(".json") or member in ("Report/Layout", "DataModelSchema")


def _validate_tmdl(text: str) -> None:
    fences = 0
    for number, line in enumerate(text.splitlines(), 1):
        indent = line[: len(line) - len(line.lstrip())]
        if " " in indent and "\t" in indent:
            raise ValueError(f"TMDL line {number} mixes tabs and spaces in its indentation.")
        if line.strip().startswith("```"):
            fences += 1
    if fences % 2:
        raise ValueError("TMDL has an unclosed code-fence block.")


def make_diff(before: str, after: str, label: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{label}", tofile=f"b/{label}", n=3,
    ))
