"""Where edits are allowed. Writes only happen inside PBI_MCP_EDIT_ROOT, a folder the
user created for copies. Originals live elsewhere, so they cannot be reached by an edit
even if the model is talked into trying."""
from __future__ import annotations

import os
from pathlib import Path

from .formats import SUPPORTED_TEXT, classify

ENV_ROOT = "PBI_MCP_EDIT_ROOT"
CHANGES_DIR = ".pbi-changes"


def _inside(child: Path, parent: Path) -> bool:
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _resolve(path: str) -> Path:
    if not path or not path.strip():
        raise ValueError("path is required.")
    p = Path(path.strip()).expanduser()
    if not p.is_absolute():
        raise ValueError(f"path must be absolute (got {path!r}).")
    return p.resolve()


def edit_root() -> Path:
    raw = os.environ.get(ENV_ROOT, "").strip()
    if not raw:
        raise ValueError(f"Editing is off: set {ENV_ROOT} to the folder that holds your COPIES of the files, then restart the server.")
    root = Path(raw).expanduser().resolve()
    home = Path.home().resolve()
    if not root.is_dir():
        raise ValueError(f"{ENV_ROOT} is not an existing folder: {root}")
    if root == home or root.parent == root or not _inside(root, home):
        raise ValueError(f"{ENV_ROOT} must be a dedicated folder inside {home}, not the profile itself or a drive root.")
    return root


def resolve_edit_target(path: str) -> Path:
    """An existing supported file inside the edit root (never a symlink, never the change store)."""
    target = _resolve(path)
    root = edit_root()
    if not _inside(target, root):
        raise ValueError(f"Edits are only allowed inside the edit folder ({root}). Make a copy there first; originals are never edited.")
    if _inside(target, root / CHANGES_DIR):
        raise ValueError("The change store cannot be edited.")
    if not target.is_file():
        raise ValueError(f"No such file: {target}. Give the path of a file, not a folder.")
    if Path(path.strip()).is_symlink():
        raise ValueError("Symbolic links are not edited.")
    classify(target)
    return target


def resolve_review_target(path: str) -> Path:
    """An existing supported file anywhere under the user's profile (read only)."""
    target = _resolve(path)
    home = Path.home().resolve()
    if not _inside(target, home):
        raise ValueError(f"Only files inside {home} can be reviewed.")
    if not target.is_file():
        raise ValueError(f"No such file: {target}. Supported: {SUPPORTED_TEXT}.")
    classify(target)
    return target


def is_in_edit_root(target: Path) -> bool:
    try:
        return _inside(target, edit_root())
    except ValueError:
        return False
