"""Locate, describe, copy and scaffold Power BI Project (.pbip) folders."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Files that are machine-local caches, never part of the project definition.
CACHE_NAMES = {"cache.abf"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pbi-plus"}


class ProjectError(ValueError):
    pass


@dataclass
class Project:
    root: Path
    name: str
    pbip: Path | None
    model_dir: Path | None
    report_dir: Path | None

    @property
    def tmdl_dir(self) -> Path | None:
        if self.model_dir and (self.model_dir / "definition").is_dir():
            return self.model_dir / "definition"
        return None

    @property
    def bim_file(self) -> Path | None:
        if self.model_dir and (self.model_dir / "model.bim").is_file():
            return self.model_dir / "model.bim"
        return None

    @property
    def model_format(self) -> str | None:
        if self.tmdl_dir:
            return "tmdl"
        if self.bim_file:
            return "bim"
        return None

    @property
    def pbir_dir(self) -> Path | None:
        if self.report_dir and (self.report_dir / "definition" / "pages").is_dir():
            return self.report_dir / "definition"
        return None

    @property
    def report_format(self) -> str | None:
        if self.pbir_dir:
            return "pbir"
        if self.report_dir and (self.report_dir / "report.json").is_file():
            return "pbir-legacy"
        return None

    def record(self) -> dict[str, Any]:
        """Exact folders of this project, so later steps never have to re-guess them from the root."""
        return {"root": str(self.root), "name": self.name, "pbip": str(self.pbip) if self.pbip else None,
                "model_dir": str(self.model_dir) if self.model_dir else None, "report_dir": str(self.report_dir) if self.report_dir else None}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "Project":
        def p(v):
            return Path(v) if v else None
        return cls(Path(rec["root"]), rec["name"], p(rec.get("pbip")), p(rec.get("model_dir")), p(rec.get("report_dir")))

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "root": str(self.root), "pbip": str(self.pbip) if self.pbip else None,
            "semantic_model_dir": str(self.model_dir) if self.model_dir else None, "model_format": self.model_format,
            "report_dir": str(self.report_dir) if self.report_dir else None, "report_format": self.report_format,
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _report_to_model(report_dir: Path) -> Path | None:
    pbir = report_dir / "definition.pbir"
    if not pbir.is_file():
        return None
    try:
        data = _read_json(pbir)
    except (OSError, ValueError):
        return None
    ref = (data.get("datasetReference") or {}).get("byPath", {}).get("path")
    if ref:
        cand = (report_dir / ref).resolve()
        if cand.is_dir():
            return cand
    return None


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def check_folder_name(name: str) -> str:
    """A plain folder name: no separators, dots-only, control characters, trailing dot/space, or Windows device names."""
    if (not name or name != name.strip() or name.endswith(".") or set(name) <= {"."} or any(ord(c) < 32 for c in name)
            or any(c in name for c in '\\/:*?"<>|') or name.split(".")[0].lower() in _RESERVED or len(name) > 100):
        raise ProjectError("The name must be a plain folder name (no path characters, no trailing dot or space, not a Windows device name).")
    return name


def locate(path: str) -> Project:
    """Resolve a .pbip file, a project folder, a *.Report or *.SemanticModel folder, or a
    definition.pbir path into a Project. Never guesses between several projects."""
    if not path or not str(path).strip():
        raise ProjectError("path is required.")
    p = Path(str(path).strip()).expanduser()
    if not p.exists():
        raise ProjectError(f"No such path: {p}")
    p = p.resolve()
    pbip: Path | None = None
    if p.is_file():
        if p.suffix.lower() == ".pbip":
            pbip = p
            root = p.parent
        elif p.name.lower() == "definition.pbir":
            root = p.parent.parent
            report = p.parent
            return Project(root, report.name.removesuffix(".Report"), None, _report_to_model(report), report)
        else:
            raise ProjectError(f"Expected a .pbip file or a project folder, got {p.name}.")
    elif p.name.endswith(".SemanticModel"):
        return Project(p.parent, p.name.removesuffix(".SemanticModel"), None, p, _sibling(p.parent, p.name.removesuffix(".SemanticModel") + ".Report"))
    elif p.name.endswith(".Report"):
        model = _report_to_model(p) or _sibling(p.parent, p.name.removesuffix(".Report") + ".SemanticModel")
        return Project(p.parent, p.name.removesuffix(".Report"), None, model, p)
    else:
        root = p
        pbips = sorted(root.glob("*.pbip"))
        if len(pbips) == 1:
            pbip = pbips[0]
        elif len(pbips) > 1:
            raise ProjectError(f"{root} holds several .pbip files ({', '.join(x.name for x in pbips)}); pass one.")
    name = pbip.stem if pbip else root.name
    report = _sibling(root, f"{name}.Report")
    model = _sibling(root, f"{name}.SemanticModel")
    if report is None and model is None:
        reports = sorted(root.glob("*.Report"))
        models = sorted(root.glob("*.SemanticModel"))
        if len(reports) == 1:
            report = reports[0]
        if len(models) == 1:
            model = models[0]
        if report is None and model is None:
            raise ProjectError(f"{root} has no *.Report or *.SemanticModel folder: not a Power BI project.")
    if model is None and report is not None:
        model = _report_to_model(report)
    return Project(root, name, pbip, model, report)


def _sibling(parent: Path, name: str) -> Path | None:
    cand = parent / name
    return cand if cand.is_dir() else None


def find_projects(search_dirs: list[str], max_results: int = 200, max_depth: int = 6) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    truncated = False
    for raw in search_dirs:
        base = Path(raw).expanduser()
        if not base.is_dir():
            missing.append(str(base))
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and len(Path(dirpath, d).parts) - base_depth <= max_depth]
            for fn in filenames:
                if fn.lower().endswith(".pbip"):
                    if len(found) >= max_results:
                        truncated = True
                        break
                    full = Path(dirpath, fn)
                    try:
                        found.append({**locate(str(full)).describe(), "modified": full.stat().st_mtime})
                    except ProjectError as exc:
                        found.append({"pbip": str(full), "error": str(exc)})
            if truncated:
                break
    found.sort(key=lambda r: r.get("modified", 0), reverse=True)
    return {"projects": found, "count": len(found), "truncated": truncated, "missing_dirs": missing}


def copy_project(src: str, dest_parent: str, new_name: str | None = None) -> dict[str, Any]:
    """Copy a project to `dest_parent/<new_name>` (cache.abf and .pbi-plus are not copied).
    Refuses to overwrite. Returns the new project description."""
    from . import changes
    proj = locate(src)
    parent = Path(dest_parent).expanduser().resolve()
    if not parent.is_dir():
        raise ProjectError(f"Destination folder does not exist: {parent}")
    name = (new_name or f"{proj.name}-copy").strip()
    check_folder_name(name)
    try:
        changes.check_write_target(parent / name)
    except changes.ChangeError as exc:
        raise ProjectError(str(exc)) from exc
    target = parent / name
    if target.exists():
        raise ProjectError(f"{target} already exists; choose another name. Nothing was copied.")
    if str(target).lower().startswith(str(proj.root).lower() + os.sep):
        raise ProjectError("Destination cannot be inside the project being copied.")
    old_name = proj.name
    shutil.copytree(proj.root, target, ignore=_ignore)
    # Rename the project-named entries so Desktop sees a coherent project.
    if name != old_name:
        for entry in list(target.iterdir()):
            if entry.name == f"{old_name}.pbip":
                entry.rename(target / f"{name}.pbip")
            elif entry.name == f"{old_name}.Report":
                entry.rename(target / f"{name}.Report")
            elif entry.name == f"{old_name}.SemanticModel":
                entry.rename(target / f"{name}.SemanticModel")
        _repoint(target, old_name, name)
    return locate(str(target)).describe()


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in CACHE_NAMES or n in SKIP_DIRS}


def _repoint(root: Path, old: str, new: str) -> None:
    pbip = root / f"{new}.pbip"
    if pbip.is_file():
        text = pbip.read_text(encoding="utf-8-sig").replace(f"{old}.Report", f"{new}.Report").replace(f"{old}.SemanticModel", f"{new}.SemanticModel")
        pbip.write_text(text, encoding="utf-8")
    pbir = root / f"{new}.Report" / "definition.pbir"
    if pbir.is_file():
        text = pbir.read_text(encoding="utf-8-sig").replace(f"{old}.SemanticModel", f"{new}.SemanticModel")
        pbir.write_text(text, encoding="utf-8")


def read_all_text(root: Path) -> dict[str, str]:
    """Every definition text file of a folder, keyed by posix path relative to `root`."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in CACHE_NAMES:
                continue
            full = Path(dirpath, fn)
            if full.suffix.lower() in (".tmdl", ".json", ".pbir", ".pbism", ".pbip", ".platform", ".bim"):
                out[full.relative_to(root).as_posix()] = full.read_text(encoding="utf-8-sig")
    return out
