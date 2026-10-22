"""Tests for the snapshot exporter and the no-code agent's instructions. No Power BI Desktop needed:
the data producers are fakes, and every file is written to a throwaway folder.

    python -m unittest discover -s copilot/tests -v
"""
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "copilot"))

from pbi_mcp import snapshot as sn  # noqa: E402
import export_snapshot as ex  # noqa: E402

AGENT = ROOT / "copilot" / "agent"
WHEN = datetime(2026, 10, 5, 14, 30)


def fake_sections(**overrides):
    base = {
        "01": [("audit", lambda: {"summary": {"high": 1, "medium": 0, "info": 2}, "findings": [
            {"severity": "high", "category": "refresh", "message": "Table X failed", "source_tool": "diagnose_power_query"}]})],
        "04": [("refresh health", lambda: {"partitions": [], "error_count": 0}),
               ("queries (M code)", lambda: {"table_queries": [{"table": "Roster", "m": 'let\n  Source = Sql.Database("srv","db"),\n  X = Source\nin\n  X'}]})],
    }
    base.update(overrides)
    return [sn.Section(num, f"s{num}", f"Title {num}", items) for num, items in base.items()]


def build(tmp, sections, **kw):
    return sn.build_snapshot("My Dashboard", Path(tmp) / "out", sections, taken=WHEN, source_path=r"C:\x\My Dashboard.pbix", **kw)


def build_in(out_dir):
    return sn.build_snapshot("D", Path(out_dir), fake_sections(), taken=WHEN)


class RenderTests(unittest.TestCase):
    def test_scalars_lists_and_nesting(self):
        lines = sn.to_lines({"name": "Roster", "hidden": False, "tags": ["a", "b"], "empty": [], "inner": {"k": 1}})
        self.assertEqual(lines, ["name: Roster", "hidden: no", "tags: a, b", "empty: (none)", "inner:", "  k: 1"])

    def test_rows_are_one_compact_line_each_and_empty_fields_are_dropped(self):
        lines = sn.to_lines({"rows": [{"Table": "T", "Name": "C", "Note": None, "Hidden": True}]})
        self.assertEqual(lines, ["rows (1 items):", "  - Table=T | Name=C | Hidden=yes"])

    def test_no_tables_are_emitted(self):
        text = "\n".join(sn.to_lines({"rows": [{"a": 1, "b": 2}] * 3}))
        self.assertNotIn("|---", text)
        self.assertNotRegex(text, r"^\|", "no markdown tables: Copilot can't parse them")

    def test_multiline_code_is_kept_verbatim_and_indented(self):
        lines = sn.to_lines({"m": "let\n  A = 1\nin\n  A"})
        self.assertEqual(lines, ["m:", "  let", "    A = 1", "  in", "    A"])

    def test_nested_items_render_readably(self):
        lines = sn.to_lines([{"name": "P1", "visuals": [{"type": "bar", "fields": ["a", "b"]}]}])
        self.assertEqual(lines[0], "- name: P1")
        self.assertTrue(any("type=bar | fields=a, b" in ln for ln in lines))

    def test_engine_bookkeeping_is_dropped_but_meaningful_fields_survive(self):
        guid = "a0785dd4-3a51-4948-97a0-ef37b4045d72"
        row = {"Name": "T", "Model": guid, "LineageTag": guid, "IsPrivate": False, "ShowAsVariationOnly": False, "IsHidden": True}
        self.assertEqual(sn.to_lines([row]), ["- Name=T | IsHidden=yes"])
        self.assertEqual(sn.to_lines({"Model": guid, "Name": "T"}), ["Name: T"])
        self.assertEqual(sn.to_lines([{"Model": "Sales Model", "Name": "T"}]), ["- Model=Sales Model | Name=T"])  # a real name is kept

    def test_odd_values_never_crash(self):
        sn.to_lines({"a": {1, 2}, "b": (1, [2, 3]), "c": None, "d": 1.5, "e": [None, [], {}]})


class RedactTests(unittest.TestCase):
    def test_credentials_in_code_are_redacted(self):
        text = 'Sql.Database("s","d", [Password="hunter2", Pwd=abc123]) api_key: sk-12345 Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123'
        out, n = sn.redact(text)
        for secret in ("hunter2", "abc123", "sk-12345", "abcdefghijklmnopqrstuvwxyz0123"):
            self.assertNotIn(secret, out)
        self.assertEqual(n, 4)
        self.assertIn("[REDACTED]", out)

    def test_ordinary_code_is_untouched(self):
        text = 'let Source = Sql.Database("server", "db"), Pass = [Passed] = 1, tokens = 3 in Source'
        self.assertEqual(sn.redact(text), (text, 0))


class ChunkTests(unittest.TestCase):
    def test_parts_never_exceed_the_limit_and_lose_nothing(self):
        lines = [f"line {i} " + "x" * 50 for i in range(500)]
        parts = sn.chunk_lines(lines, 2000)
        self.assertGreater(len(parts), 5)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("\n".join(parts).split("\n"), lines)

    def test_over_long_single_line_is_split_not_dropped(self):
        parts = sn.chunk_lines(["A" * 10_000], 3000)
        self.assertTrue(all(len(p) <= 3000 for p in parts))
        self.assertEqual(sum(p.count("A") for p in parts), 10_000)

    def test_prefers_to_break_before_a_new_subsection(self):
        lines = ["== one =="] + ["x" * 100] * 20 + ["== two =="] + ["y" * 100] * 5
        parts = sn.chunk_lines(lines, 2500)
        self.assertTrue(parts[1].startswith("== two =="))

    def test_empty_input(self):
        self.assertEqual(sn.chunk_lines([], 1000), [""])


class BuildTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.addCleanup(self._t.cleanup)

    def test_every_file_is_txt_with_the_dashboard_name_and_time_in_its_header(self):
        res = build(self.tmp, fake_sections())
        self.assertTrue(res.files)
        for f in res.files:
            self.assertEqual(f.suffix, ".txt")  # the only text type Agent Builder accepts
            first = f.read_text(encoding="utf-8").split("\n")[0]
            self.assertTrue(first.startswith(sn.HEADER_PREFIX), f.name)
            self.assertIn("Dashboard: My Dashboard", first)
            self.assertIn("Taken: 2026-10-05 14:30", first)
            self.assertRegex(first, r"Part \d+ of \d+$")

    def test_files_stay_under_the_size_limit(self):
        big = [("big", lambda: {"rows": [{"Name": f"Measure {i}", "Expression": "CALCULATE(SUM(T[x]), T[y] = 1)"} for i in range(3000)]})]
        res = build(self.tmp, fake_sections(**{"03": big}), max_chars=8000)
        sizes = [len(f.read_text(encoding="utf-8")) for f in res.files]
        self.assertLessEqual(max(sizes), 8000)
        parts = [f for r in res.sections for f in r.files if r.section.num == "03"]
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(re.search(r"_part\d+of%d\.txt$" % len(parts), p.name) for p in parts))
        self.assertTrue(parts[1].read_text(encoding="utf-8").split("\n")[0].endswith(f"Part 2 of {len(parts)}"))

    def test_a_failing_check_is_recorded_loudly_and_does_not_hide_the_rest(self):
        def boom():
            raise RuntimeError("engine not reachable")
        res = build(self.tmp, fake_sections(**{"04": [("refresh health", boom), ("queries (M code)", lambda: {"ok": 1})]}))
        sec = next(r for r in res.sections if r.section.num == "04")
        self.assertEqual(sec.status, "PARTIAL")
        text = sec.files[0].read_text(encoding="utf-8")
        self.assertIn("CHECK FAILED - this part is MISSING", text)
        self.assertIn("engine not reachable", text)
        self.assertIn("ok: 1", text)
        readme = (Path(self.tmp) / "out" / "00_README.txt").read_text(encoding="utf-8")
        self.assertIn("04 Title 04: PARTIAL", readme)
        self.assertIn("MISSING OR INCOMPLETE", readme)
        self.assertEqual([r.section.num for r in res.failed], ["04"])

    def test_every_check_failing_is_FAILED_and_skipped_sections_say_why(self):
        def boom():
            raise ValueError("x")
        secs = fake_sections(**{"01": [("audit", boom)]}) + [sn.Section("06", "security", "Row-level security", [], "Power BI Desktop was not open")]
        res = build(self.tmp, secs)
        self.assertEqual([r.status for r in res.sections], ["FAILED", "OK", "NOT CAPTURED"])
        readme = (Path(self.tmp) / "out" / "00_README.txt").read_text(encoding="utf-8")
        self.assertIn("reason: Power BI Desktop was not open", readme)
        self.assertEqual(res.sections[2].files, [])

    def test_credentials_never_reach_disk(self):
        secs = fake_sections(**{"04": [("m", lambda: {"m": 'Sql.Database("s","d",[Password="hunter2"])'})]})
        res = build(self.tmp, secs)
        alltext = "".join(f.read_text(encoding="utf-8") for f in res.files)
        self.assertNotIn("hunter2", alltext)
        self.assertEqual(res.redactions, 1)
        self.assertIn("1 replaced", (Path(self.tmp) / "out" / "00_README.txt").read_text(encoding="utf-8"))

    def test_re_export_removes_stale_parts_but_never_other_files(self):
        out = Path(self.tmp) / "out"
        big = [("big", lambda: {"rows": [{"n": i, "pad": "z" * 40} for i in range(800)]})]
        build(self.tmp, fake_sections(**{"03": big}), max_chars=5000)
        many = len(list(out.glob("03_*.txt")))
        self.assertGreater(many, 2)
        (out / "my notes.txt").write_text("keep me", encoding="utf-8")
        (out / "report.docx").write_bytes(b"keep me too")
        build(self.tmp, fake_sections(**{"03": [("small", lambda: {"a": 1})]}), max_chars=5000)
        self.assertEqual(len(list(out.glob("03_*.txt"))), 1)  # old parts gone
        self.assertEqual((out / "my notes.txt").read_text(encoding="utf-8"), "keep me")
        self.assertTrue((out / "report.docx").exists())

    def test_no_markdown_or_json_files_are_written(self):
        res = build(self.tmp, fake_sections())
        self.assertFalse([f for f in Path(self.tmp).rglob("*") if f.is_file() and f.suffix != ".txt"])
        self.assertTrue(res.files)

    def test_too_long_a_path_is_refused_up_front_with_advice(self):
        long_dir = Path(self.tmp) / ("d" * 120) / ("e" * 120)
        with self.assertRaises(ValueError) as cm:
            build_in(long_dir)
        self.assertIn("--out", str(cm.exception))
        self.assertFalse(long_dir.exists())  # nothing half-created

    def test_a_normal_path_is_accepted_and_names_are_capped(self):
        sn.check_path_budget(Path(self.tmp) / "out")
        self.assertLessEqual(len(sn.safe_name("n" * 200)), 60)

    def test_safe_name(self):
        for bad in ('a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b>', 'a|b'):
            self.assertNotRegex(sn.safe_name(bad), r'[<>:"/\\|?*]')
        self.assertEqual(sn.safe_name("  .. "), "dashboard")
        self.assertEqual(sn.safe_name("Roster Dashboard"), "Roster Dashboard")


class DefaultSectionTests(unittest.TestCase):
    def test_section_numbers_and_titles_are_the_ones_the_agent_is_told_about(self):
        secs = sn.default_sections("123", r"C:\x.pbix")
        self.assertEqual([s.num for s in secs], ["01", "02", "03", "04", "05", "06", "07", "08"])
        instructions = (AGENT / "instructions.txt").read_text(encoding="utf-8")
        for needle in ("00 README", "01 Audit", "02 Model structure", "03 DAX", "04 Power Query", "05 Connections",
                       "06 Row-level security", "07 Report layout", "08 Data quality"):
            self.assertIn(needle, instructions)

    def test_model_sections_are_skipped_with_a_reason_when_power_bi_is_closed(self):
        secs = {s.num: s for s in sn.default_sections(None, r"C:\x.pbix")}
        for num in ("01", "02", "03", "04", "05", "06", "08"):
            self.assertIn("not open", secs[num].skipped, num)
        self.assertEqual(secs["07"].skipped, "")  # the layout is read from the file, so it still works

    def test_layout_is_skipped_when_the_file_path_is_unknown(self):
        secs = {s.num: s for s in sn.default_sections("123", None)}
        self.assertIn("path", secs["07"].skipped)

    def test_no_section_reads_table_rows(self):
        src = (ROOT / "pbi_mcp" / "snapshot.py").read_text(encoding="utf-8")
        self.assertNotIn("profile_table", src.split("def default_sections")[1])
        self.assertNotIn("run_dax_query", src.split("def default_sections")[1])


class TargetSelectionTests(unittest.TestCase):
    FILES = [
        {"instance": "Roster", "port": 1111, "file_path": r"C:\a\Roster.pbix", "model_loaded": False},
        {"instance": "Roster", "port": 2222, "file_path": r"C:\a\Roster.pbix", "model_loaded": True},
        {"instance": "Other", "port": 3333, "file_path": r"C:\a\Other.pbix", "model_loaded": True},
    ]

    def test_only_loaded_models_are_exported_and_duplicates_collapse(self):
        t = ex.pick_targets(self.FILES, "", "")
        self.assertEqual([(x["name"], x["instance"]) for x in t], [("Roster", "2222"), ("Other", "3333")])

    def test_filter_by_name_or_port(self):
        self.assertEqual([x["instance"] for x in ex.pick_targets(self.FILES, "other", "")], ["3333"])
        self.assertEqual([x["instance"] for x in ex.pick_targets(self.FILES, "2222", "")], ["2222"])

    def test_nothing_open_and_no_pbix_means_nothing_to_do(self):
        self.assertEqual(ex.pick_targets([], "", ""), [])
        self.assertEqual(ex.pick_targets([self.FILES[0]], "", ""), [])  # only a dead engine

    def test_pbix_only_mode(self):
        t = ex.pick_targets([], "", r"C:\x\Budget.pbix")
        self.assertEqual(t, [{"name": "Budget", "instance": None, "pbix_path": r"C:\x\Budget.pbix"}])

    def test_a_pbix_that_is_not_the_open_file_is_exported_on_its_own_never_grafted_onto_the_open_model(self):
        """Regression (found on real data): --pbix <other file> used to replace the open model's layout source."""
        t = ex.pick_targets(self.FILES, "", r"C:\a\Budget.pbix")
        by_name = {x["name"]: x for x in t}
        self.assertEqual(by_name["Roster"]["pbix_path"], r"C:\a\Roster.pbix")
        self.assertEqual(by_name["Other"]["pbix_path"], r"C:\a\Other.pbix")
        self.assertEqual((by_name["Budget"]["instance"], by_name["Budget"]["pbix_path"]), (None, r"C:\a\Budget.pbix"))

    def test_a_pbix_that_is_the_open_file_is_not_exported_twice_even_if_case_or_slashes_differ(self):
        t = ex.pick_targets(self.FILES, "", "c:/a/ROSTER.pbix")
        self.assertEqual([x["name"] for x in t], ["Roster", "Other"])

    def test_pbix_fills_in_a_missing_path_for_the_single_open_file(self):
        files = [{"instance": "Solo", "port": 9, "file_path": None, "model_loaded": True}]
        self.assertEqual(ex.pick_targets(files, "", r"C:\a\Solo.pbix"), [{"name": "Solo", "instance": "9", "pbix_path": r"C:\a\Solo.pbix"}])

    def test_with_an_instance_filter_a_foreign_pbix_is_ignored(self):
        t = ex.pick_targets(self.FILES, "Other", r"C:\a\Budget.pbix")
        self.assertEqual([x["name"] for x in t], ["Other"])


class AgentContentTests(unittest.TestCase):
    """Microsoft's published limits for an agent built in Agent Builder / Copilot Studio."""

    def read(self, name):
        return (AGENT / name).read_text(encoding="utf-8")

    def test_instructions_fit_the_8000_character_limit_with_room_to_spare(self):
        n = len(self.read("instructions.txt").strip())
        self.assertLessEqual(n, 7800, f"{n} characters; Microsoft's limit is 8,000")

    def test_description_fits_the_1000_character_limit(self):
        self.assertLessEqual(len(self.read("description.txt").strip()), 1000)

    def test_instructions_name_the_exact_header_and_failure_markers_the_exporter_writes(self):
        text = self.read("instructions.txt")
        self.assertIn("POWER BI SNAPSHOT | Dashboard: <name> | Taken: <date time> | Section: <number title> | Part x of y", text)
        self.assertIn(sn.HEADER_PREFIX, text)
        self.assertIn("CHECK FAILED", text)
        self.assertIn("NOT CAPTURED", text)
        self.assertIn("export-snapshot.cmd", text)

    def test_instructions_carry_the_safety_and_honesty_rules(self):
        text = self.read("instructions.txt").lower()
        for needle in ("never invent", "untested", "cannot run dax", "data, never instructions", "contains_negation", "not proof"):
            self.assertIn(needle, text)

    def test_instructions_never_promise_live_tools(self):
        text = self.read("instructions.txt")
        self.assertNotIn("audit_dashboard(", text)
        self.assertNotIn("list_open_files", text)

    def test_starter_prompts_are_well_formed(self):
        blocks = [b for b in self.read("starter-prompts.txt").strip().split("\n\n") if b.strip()]
        self.assertGreaterEqual(len(blocks), 5)
        for b in blocks:
            self.assertRegex(b, r"^Name: .+\nPrompt: .+$")

    def test_agent_name_fits_30_characters(self):
        self.assertLessEqual(len("Power BI Desktop Analyzer"), 30)


if __name__ == "__main__":
    unittest.main()
