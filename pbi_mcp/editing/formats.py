"""File kinds the editor understands, and which parts of each may be edited.

Only text parts are ever edited. The compressed model inside a .pbix (`DataModel`),
the Power Query container (`DataMashup`) and the signing/metadata parts are never
touched: rewriting them corrupts the file or silently drops data.
"""
from __future__ import annotations

import re
from pathlib import Path

SUFFIX_KIND = {
    ".pbix": "pbix",
    ".pbit": "pbit",
    ".pbip": "pbip",
    ".pbir": "pbir",
    ".pbids": "pbids",
    ".rdl": "rdl",
    ".tmdl": "tmdl",
}
CONTAINER_KINDS = frozenset({"pbix", "pbit"})
JSON_KINDS = frozenset({"pbip", "pbir", "pbids"})
SUPPORTED_TEXT = ".pbix, .pbit, .pbip, .pbir, .pbids, .rdl, .tmdl, and .json files inside a *.Report folder (PBIR)"

_REPORT_PARTS = (re.compile(r"^Report/Layout$"), re.compile(r"^Report/definition/.+\.json$"))
_EDITABLE_MEMBERS = {
    "pbix": _REPORT_PARTS,
    "pbit": _REPORT_PARTS + (re.compile(r"^DataModelSchema$"),),
}

RISK_NOTES = {
    "pbix": "EXPERIMENTAL: Microsoft does not document editing a .pbix outside Power BI Desktop (not verified). Only report-layer text parts are editable, on a copy. Open the file in Desktop to confirm it loads.",
    "pbit": "EXPERIMENTAL: external edits of a .pbit are not documented by Microsoft (not verified). Open it in Desktop to confirm it loads.",
    "pbip": "Changes which report/model folders this project points at. A wrong path makes the project fail to open.",
    "pbir": "Changes report pages, visuals or formatting. A bad reference makes a visual or page fail to load.",
    "pbids": "Changes where a data source points. Check privacy levels and credentials after reopening.",
    "rdl": "Changes a paginated report. It only takes effect after the report is re-published (which this tool never does).",
    "tmdl": "Changes the semantic model definition. Dependent measures or relationships can break; reopen in Desktop and run an audit.",
}


def classify(path: Path) -> str:
    """Kind of file at `path`, or ValueError naming what is supported."""
    kind = SUFFIX_KIND.get(path.suffix.lower())
    if kind:
        return kind
    if path.suffix.lower() == ".json" and any(p.name.lower().endswith(".report") for p in path.parents):
        return "pbir"
    raise ValueError(f"Unsupported file type {path.suffix or path.name!r}. Supported: {SUPPORTED_TEXT}.")


def member_is_editable(kind: str, member: str) -> bool:
    return any(p.match(member) for p in _EDITABLE_MEMBERS.get(kind, ()))
