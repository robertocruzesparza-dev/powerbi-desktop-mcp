"""Copy the bundled Power BI skills into the folder an AI client reads skills from.

    python scripts/install_skills.py --target claude          # ~/.claude/skills
    python scripts/install_skills.py --target gemini          # ~/.gemini/skills
    python scripts/install_skills.py --target copilot --project .   # <project>/.github/skills
    python scripts/install_skills.py --target all --dry-run

Each skill is a folder with a SKILL.md (the open Agent Skills layout). Existing skill folders with the same name
are replaced; nothing else in the destination is touched.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"

# (user-level folder, project-level folder relative to the project)
DESTINATIONS = {
    "claude": (Path.home() / ".claude" / "skills", Path(".claude") / "skills"),
    "gemini": (Path.home() / ".gemini" / "skills", Path(".gemini") / "skills"),
    "copilot": (Path.home() / ".copilot" / "skills", Path(".github") / "skills"),
}


def skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def destination(target: str, project: str | None) -> Path:
    user_dir, project_dir = DESTINATIONS[target]
    return Path(project).resolve() / project_dir if project else user_dir


def install(target: str, project: str | None = None, dry_run: bool = False) -> list[str]:
    dest = destination(target, project)
    done = []
    for src in skill_dirs():
        done.append(f"{src.name} -> {dest / src.name}")
        if dry_run:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest / src.name, ignore_errors=True)
        shutil.copytree(src, dest / src.name, ignore=shutil.ignore_patterns("__pycache__"))
    registry = SKILLS / "POWERBI-REGISTRY.md"
    if registry.is_file() and not dry_run:
        shutil.copy2(registry, dest / registry.name)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=[*DESTINATIONS, "all"], required=True)
    ap.add_argument("--project", help="install into this project folder instead of the user-level folder")
    ap.add_argument("--dry-run", action="store_true", help="show what would be copied")
    args = ap.parse_args(argv)
    for target in (DESTINATIONS if args.target == "all" else [args.target]):
        for line in install(target, args.project, args.dry_run):
            print(f"[{target}] {line}")
    if args.dry_run:
        print("dry run: nothing copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
