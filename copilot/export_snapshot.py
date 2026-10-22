"""Export a plain-text snapshot of your open Power BI dashboard(s) for a Microsoft 365 Copilot agent.

    export-snapshot.cmd                      every open file that has finished loading
    export-snapshot.cmd --instance "Roster Dashboard"
    export-snapshot.cmd --pbix "C:\\path\\File.pbix"     report layout only (no need to open Power BI)
    export-snapshot.cmd --out "C:\\...\\Power BI Snapshots"

Run it with the dashboard open in Power BI Desktop (and finished loading). It reads only; it never changes,
saves or refreshes a file and exports no table rows. Output: one folder per dashboard of small .txt files,
default <Add-In Tools>\\Power BI Snapshots\\<dashboard>\\ -- put that folder in SharePoint/OneDrive (it is already
there if your Add-In Tools folder syncs) and point your Copilot agent's knowledge at it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pbi_mcp import snapshot  # noqa: E402

DEFAULT_OUT = ROOT.parent / "Power BI Snapshots"


def tool_version() -> str:
    try:
        return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""


def pick_targets(open_files: list[dict], instance: str, pbix: str) -> list[dict]:
    """Which dashboards to export: [{name, instance (or None), pbix_path (or None)}]."""
    loaded = [f for f in open_files if f.get("model_loaded")]
    if instance:
        wanted = instance.strip().lower()
        loaded = [f for f in loaded if wanted in (str(f["instance"]).lower(), str(f["port"]))]
    targets, seen = [], set()
    for f in loaded:
        key = (f.get("file_path") or f["instance"]).lower()
        if key in seen:  # the same file can show up twice (a dead engine plus the real one)
            continue
        seen.add(key)
        targets.append({"name": f["instance"], "instance": str(f["port"]), "pbix_path": f.get("file_path")})
    if pbix:
        same = lambda a, b: bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))  # noqa: E731
        unknown_path = [t for t in targets if not t["pbix_path"]]
        if len(targets) == 1 and unknown_path:  # an open file whose path Power BI didn't report
            unknown_path[0]["pbix_path"] = pbix
        elif not any(same(t["pbix_path"], pbix) for t in targets) and not instance:
            # never graft this file's layout onto a different open model: export it on its own, layout only
            targets.append({"name": Path(pbix).stem, "instance": None, "pbix_path": pbix})
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default="", help="export only this open file (name or port from list_open_files)")
    ap.add_argument("--pbix", default="", help="path to a .pbix; with Power BI closed this exports the report layout only")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"parent folder (default: {DEFAULT_OUT})")
    ap.add_argument("--max-chars", type=int, default=snapshot.DEFAULT_MAX_CHARS, help="largest .txt file (Microsoft advises about 36,000)")
    ap.add_argument("--no-quality", action="store_true", help="skip the relationship-integrity and unused-field checks (faster)")
    ap.add_argument("--traces", action="store_true", help="also include Power BI's own runtime logs (may mention other files)")
    args = ap.parse_args(argv)

    from pbi_mcp import server

    try:
        open_files = server.list_open_files()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not look for open Power BI files ({type(exc).__name__}: {exc}); continuing in report-layout-only mode.")
        open_files = []
    targets = pick_targets(open_files, args.instance, args.pbix)
    if not targets:
        print("No dashboard to export. Open the file in Power BI Desktop and wait until it finishes loading, then run this again.\n"
              "(Or pass --pbix <file.pbix> to export just the report layout without opening it.)")
        return 1

    exit_code = 0
    for t in targets:
        print(f"\n{t['name']}" + ("" if t["instance"] else "   [report layout only: Power BI Desktop is not open with this file]"))
        out_dir = args.out / snapshot.safe_name(t["name"])
        sections = snapshot.default_sections(t["instance"], t["pbix_path"], quality=not args.no_quality, traces=args.traces)
        started = time.time()
        try:
            result = snapshot.build_snapshot(t["name"], out_dir, sections, taken=datetime.now(), source_path=t["pbix_path"],
                                             max_chars=args.max_chars, tool_version=tool_version(), progress=print)
        except (ValueError, OSError) as exc:
            print(f"\nERROR: {exc}")
            return 1
        print(f"\nWrote {len(result.files)} files to {out_dir}  ({time.time() - started:.0f}s)")
        if result.redactions:
            print(f"Redacted {result.redactions} credential-like value(s) from code.")
        for r in result.failed:
            print(f"WARNING: section {r.section.num} {r.section.title} is {r.status}: {'; '.join(r.detail)}")
            exit_code = 2
    print("\nNext: make sure that folder has synced to SharePoint/OneDrive, then ask your Copilot agent about the dashboard.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
