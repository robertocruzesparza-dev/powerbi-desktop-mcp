"""Release hygiene: versions agree, entry points work, the stdio server lists the edit tools, and
nothing private (names of an organization, tenants, tunnels, local profile paths) is committed.

    python -m unittest discover -s copilot/tests -v
"""
import asyncio
import json
import re
import subprocess
import sys
import unittest

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pbi_mcp  # noqa: E402
from pbi_mcp import server  # noqa: E402

# Built from fragments so this file does not match its own scan.
FORBIDDEN = [
    r"(?<![A-Za-z])tus" + "d", "405" + "842", "bc7050" + "e0", "OneDrive" + " - ", "ALE Test" + "ing", "Tuc" + "son",
    "Cruz" + "e@", r"\b[a-z0-9]{7,}-\d{4}\.[a-z0-9]+\.devtunnels\.ms",
]
SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".venv", "venv"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".cmd", ".ps1", ".cfg", ".ini"}


def tracked_text_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and not SKIP_DIRS & set(path.relative_to(ROOT).parts):
            yield path


class VersionTests(unittest.TestCase):
    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_versions_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["dynamic"], ["version"])
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "copilot" / "appPackage" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], pbi_mcp.__version__)
        self.assertEqual(manifest["version"], pbi_mcp.__version__)
        self.assertIn(f"## [{pbi_mcp.__version__}]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_console_scripts_point_at_real_callables(self):
        scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        self.assertEqual(set(scripts), {"powerbi-desktop-mcp", "powerbi-desktop-mcp-http"})
        for target in scripts.values():
            module, func = target.split(":")
            self.assertTrue(callable(getattr(__import__(module, fromlist=[func]), func)), target)

    def test_required_repo_files_exist(self):
        for name in ("LICENSE", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "PRIVACY.md",
                     "THIRD-PARTY-NOTICES.md", ".gitignore", "docs/INSTALL.md", "docs/TOOLS.md", "docs/EDITING.md"):
            self.assertTrue((ROOT / name).is_file(), name)


class CommandLineTests(unittest.TestCase):
    def run_module(self, *args):
        return subprocess.run([sys.executable, "-m", "pbi_mcp", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_version_and_help_print_and_exit(self):
        out = self.run_module("--version")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, pbi_mcp.__version__))
        helped = self.run_module("--help")
        self.assertEqual(helped.returncode, 0)
        self.assertIn("PBI_MCP_EDIT_ROOT", helped.stdout)

    def test_doctor_reports_each_check_in_plain_words(self):
        out = self.run_module("--doctor")
        self.assertIn(out.returncode, (0, 1))
        self.assertIn("Python", out.stdout)
        self.assertIn("Windows", out.stdout)


class StdioServerTests(unittest.TestCase):
    def test_edit_tools_are_registered_with_the_right_hints(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertEqual(len(tools), 33)
        for name, read_only in (("review_file", True), ("propose_change", False), ("finish_change", False)):
            self.assertIn(name, tools)
            self.assertIs(tools[name].annotations.read_only_hint, read_only, name)
            self.assertGreater(len(tools[name].description or ""), 80, name)


class PrivateDataTests(unittest.TestCase):
    def test_no_private_strings_are_committed(self):
        hits = []
        for path in tracked_text_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN:
                match = re.search(pattern, text, re.I)
                if match:
                    hits.append(f"{path.relative_to(ROOT)}: {match.group(0)!r}")
        self.assertEqual(hits, [])

    def test_no_real_dashboards_are_committed(self):
        found = [p for p in ROOT.rglob("*") if p.suffix.lower() in {".pbix", ".pbit"} and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
