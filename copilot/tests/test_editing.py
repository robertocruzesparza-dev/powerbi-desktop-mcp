"""Tests for the approved-edit layer. Only synthetic fixtures: no real dashboards, no student
data, no Power BI Desktop, no network.

    python -m unittest discover -s copilot/tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pbi_mcp import editing  # noqa: E402
from pbi_mcp.editing import container, guard, store, textedit  # noqa: E402

LAYOUT = {"sections": [{"displayName": "Page One", "visualContainers": []}], "note": "alpha"}
MODEL_BYTES = b"\x01\x02compressed-model-bytes\xff\x00" * 50
RDL = ('<?xml version="1.0" encoding="utf-8"?>\n<Report><DataSources><DataSource Name="Src">'
       "<ConnectionString>Data Source=srv;Password=hunter2pass</ConnectionString></DataSource></DataSources>"
       "<Body><Title>Old title</Title></Body></Report>")
TMDL = "table Sales\n\tmeasure Total = SUM(Sales[Amount])\n\t\tformatString: 0\n"


def make_pbix(path: Path, with_schema: bool = False) -> None:
    layout = json.dumps(LAYOUT).encode("utf-16-le")  # real Report/Layout is UTF-16LE with no BOM
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Version", "1.28", compress_type=zipfile.ZIP_STORED)
        zf.writestr("Report/Layout", layout, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("DataModel", MODEL_BYTES, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("DataMashup", b"mashup-bytes", compress_type=zipfile.ZIP_STORED)
        if with_schema:
            zf.writestr("DataModelSchema", json.dumps({"name": "m", "tables": []}).encode("utf-16"), compress_type=zipfile.ZIP_DEFLATED)


class EditCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=Path.home(), prefix="pbi-edit-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.root = self.home / "copies"
        self.root.mkdir()
        self.originals = self.home / "originals"
        self.originals.mkdir()
        env = mock.patch.dict(os.environ, {guard.ENV_ROOT: str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.codes: list[str] = []
        patch = mock.patch.object(store, "announce", lambda m: self.codes.append(m.split(": ")[1].split()[0]))
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        p = self.root / name
        p.write_bytes(text.encode(encoding))
        return p

    def approve(self, change_id: str, action: str = "apply"):
        editing.finish(change_id, action, "")  # asks for a code
        return editing.finish(change_id, action, self.codes[-1])


class GuardTests(EditCase):
    def test_editing_is_off_without_a_root(self):
        with mock.patch.dict(os.environ, {guard.ENV_ROOT: ""}):
            with self.assertRaisesRegex(ValueError, "Editing is off"):
                editing.propose(str(self.root / "a.tmdl"), "a", "b", "why")

    def test_original_outside_the_edit_folder_is_refused(self):
        original = self.originals / "orig.tmdl"
        original.write_text(TMDL)
        with self.assertRaisesRegex(ValueError, "only allowed inside the edit folder"):
            editing.propose(str(original), "Total", "Sum", "why")
        self.assertEqual(original.read_text(), TMDL)

    def test_root_may_not_be_the_profile_or_a_drive_root(self):
        for bad in (str(Path.home()), Path(Path.home().anchor).as_posix()):
            with mock.patch.dict(os.environ, {guard.ENV_ROOT: bad}):
                with self.assertRaises(ValueError):
                    guard.edit_root()

    def test_unsupported_type_and_folders_are_refused(self):
        (self.root / "note.txt").write_text("x")
        (self.root / "Proj.Report").mkdir()
        for target in ("note.txt", "Proj.Report"):
            with self.assertRaises(ValueError):
                editing.propose(str(self.root / target), "x", "y", "why")

    def test_relative_paths_are_refused(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            editing.propose("copies/a.tmdl", "a", "b", "why")

    def test_change_store_cannot_be_edited(self):
        self.put("a.tmdl", TMDL)
        pid = editing.propose(str(self.root / "a.tmdl"), "Total", "Sum", "why")["change_id"]
        with self.assertRaisesRegex(ValueError, "change store"):
            editing.propose(str(self.root / guard.CHANGES_DIR / "pending" / f"{pid}.json"), "a", "b", "why")


class ProposeApplyRollbackTests(EditCase):
    def test_propose_changes_nothing_and_never_leaks_the_code(self):
        path = self.put("model.tmdl", TMDL)
        before = path.read_bytes()
        result = editing.propose(str(path), "Total", "Total Sales", "Rename for clarity")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["status"], "pending")
        self.assertIn("-\tmeasure Total = SUM", result["diff"])
        self.assertIn("+\tmeasure Total Sales = SUM", result["diff"])
        self.assertEqual(len(self.codes), 1)
        self.assertNotIn(self.codes[0], json.dumps(result))

    def test_apply_needs_the_right_code_and_is_single_use(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        with self.assertRaisesRegex(ValueError, "Wrong approval code"):
            editing.finish(cid, "apply", "000000" if self.codes[-1] != "000000" else "111111")
        self.assertEqual(path.read_text(), TMDL)
        result = editing.finish(cid, "apply", self.codes[-1])
        self.assertEqual(result["status"], "applied")
        self.assertTrue(all(c["passed"] for c in result["verify"]))
        self.assertIn("Total Sales", path.read_text())
        with self.assertRaisesRegex(ValueError, "not pending"):
            editing.finish(cid, "apply", self.codes[-1])

    def test_empty_code_issues_a_fresh_code_and_applies_nothing(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        result = editing.finish(cid, "apply", "")
        self.assertEqual(result["status"], "code_issued")
        self.assertEqual(len(self.codes), 2)
        self.assertEqual(path.read_text(), TMDL)

    def test_three_wrong_codes_lock_the_change(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        wrong = "999999" if self.codes[-1] != "999999" else "888888"
        for _ in range(3):
            with self.assertRaises(ValueError):
                editing.finish(cid, "apply", wrong)
        with self.assertRaisesRegex(ValueError, "not pending"):
            editing.finish(cid, "apply", self.codes[-1])
        self.assertEqual(path.read_text(), TMDL)

    def test_file_changed_after_proposal_is_refused(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        path.write_text(TMDL + "\tmeasure Extra = 1\n")
        with self.assertRaisesRegex(ValueError, "changed after"):
            editing.finish(cid, "apply", self.codes[-1])
        self.assertNotIn("Total Sales", path.read_text())

    def test_expired_proposal_is_refused(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        with mock.patch.object(store.time, "time", return_value=store.time.time() + store.PROPOSAL_TTL + 5):
            with self.assertRaisesRegex(ValueError, "expired"):
                editing.finish(cid, "apply", self.codes[-1])

    def test_rollback_restores_the_exact_bytes_and_needs_its_own_code(self):
        path = self.put("model.tmdl", TMDL)
        original = path.read_bytes()
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        self.assertNotEqual(path.read_bytes(), original)
        apply_code = self.codes[-1]
        with self.assertRaisesRegex(ValueError, "No approval code|Wrong approval code"):
            editing.finish(cid, "rollback", apply_code)
        result = self.approve(cid, "rollback")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(path.read_bytes(), original)

    def test_rollback_refuses_to_erase_later_work(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        path.write_text(path.read_text() + "\tmeasure Later = 2\n")
        editing.finish(cid, "rollback", "")
        with self.assertRaisesRegex(ValueError, "erase later work"):
            editing.finish(cid, "rollback", self.codes[-1])
        self.assertIn("Later", path.read_text())

    def test_rejected_proposal_leaves_the_tree_identical(self):
        path = self.put("model.tmdl", TMDL)
        snapshot = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        editing.propose(str(path), "Total", "Total Sales", "why")  # user says no: nothing else is called
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, snapshot)

    def test_journal_records_hashes_not_content(self):
        path = self.put("model.tmdl", TMDL)
        cid = editing.propose(str(path), "Total", "Secret Measure Name", "why")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        journal = (self.root / guard.CHANGES_DIR / "journal.jsonl").read_text()
        self.assertIn(cid, journal)
        self.assertNotIn("Secret Measure Name", journal)


class EditRulesTests(EditCase):
    def test_match_count_must_equal_expected(self):
        path = self.put("model.tmdl", TMDL + "\tmeasure Total2 = SUM(Sales[Amount])\n")
        with self.assertRaisesRegex(ValueError, "occurs 2 time"):
            editing.propose(str(path), "SUM(Sales[Amount])", "SUM(Sales[Net])", "why")
        result = editing.propose(str(path), "SUM(Sales[Amount])", "SUM(Sales[Net])", "why", expected_count=2)
        self.assertEqual(result["replacements"], 2)

    def test_empty_noop_and_reasonless_edits_are_refused(self):
        path = self.put("model.tmdl", TMDL)
        for args in (("", "x", "why"), ("Total", "Total", "why"), ("Total", "Sum", "  ")):
            with self.assertRaises(ValueError):
                editing.propose(str(path), *args)

    def test_credentials_are_never_written(self):
        path = self.put("a.pbids", json.dumps({"connections": [{"details": {"server": "srv"}}]}))
        with self.assertRaisesRegex(ValueError, "credential"):
            editing.propose(str(path), '"server": "srv"', '"server": "srv;Password=abc12345"', "why")

    def test_invalid_results_are_refused_per_format(self):
        cases = {
            "p.pbip": (json.dumps({"version": "1.0"}), '"1.0"', '"1.0",'),
            "p.pbir": (json.dumps({"version": "4.0"}), '"4.0"', '"4.0" "x"'),
            "r.rdl": (RDL, "<Title>Old title</Title>", "<Title>Old title"),
            "m.tmdl": (TMDL, "\tmeasure", " \tmeasure"),
        }
        for name, (text, old, new) in cases.items():
            path = self.put(name, text)
            with self.assertRaises(ValueError, msg=name):
                editing.propose(str(path), old, new, "why")
            self.assertEqual(path.read_text(), text)

    def test_rdl_doctype_is_refused(self):
        path = self.put("r.rdl", RDL)
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            editing.propose(str(path), "<Report>", '<!DOCTYPE x [<!ENTITY a "b">]><Report>', "why")

    def test_every_text_kind_can_be_edited(self):
        files = {
            "p.pbip": (json.dumps({"artifacts": []}), '"artifacts": []', '"artifacts": [{"report": {"path": "R.Report"}}]'),
            "d.pbir": (json.dumps({"version": "4.0"}), '"4.0"', '"4.0", "x": 1'),
            "s.pbids": (json.dumps({"version": "0.1", "connections": []}), '"connections": []', '"connections": [{"protocol": "tds"}]'),
            "r.rdl": (RDL, "Old title", "New title"),
            "m.tmdl": (TMDL, "formatString: 0", "formatString: 0.00"),
        }
        for name, (text, old, new) in files.items():
            path = self.put(name, text)
            cid = editing.propose(str(path), old, new, "why")["change_id"]
            self.assertEqual(editing.finish(cid, "apply", self.codes[-1])["status"], "applied", name)
            self.assertIn(new, path.read_text(), name)

    def test_pbir_json_inside_a_report_folder_is_supported(self):
        folder = self.root / "Proj.Report" / "definition" / "pages" / "p1"
        folder.mkdir(parents=True)
        page = folder / "page.json"
        page.write_text(json.dumps({"displayName": "Old"}))
        cid = editing.propose(str(page), '"Old"', '"New"', "rename page")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        self.assertEqual(json.loads(page.read_text())["displayName"], "New")

    def test_encoding_and_bom_are_preserved(self):
        path = self.root / "bom.tmdl"
        path.write_bytes(b"\xef\xbb\xbf" + TMDL.encode("utf-8"))
        cid = editing.propose(str(path), "Total", "Total Sales", "why")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))


class ContainerTests(EditCase):
    def test_pbix_layout_edit_leaves_every_other_part_byte_identical(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        with zipfile.ZipFile(path) as zf:
            others = {n: zf.read(n) for n in zf.namelist() if n != "Report/Layout"}
            order = zf.namelist()
        cid = editing.propose(str(path), '"alpha"', '"beta"', "fix note", member="Report/Layout")["change_id"]
        result = editing.finish(cid, "apply", self.codes[-1])
        self.assertTrue(all(c["passed"] for c in result["verify"]), result["verify"])
        with zipfile.ZipFile(path) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(zf.namelist(), order)
            self.assertEqual({n: zf.read(n) for n in zf.namelist() if n != "Report/Layout"}, others)
            self.assertEqual(json.loads(zf.read("Report/Layout").decode("utf-16-le"))["note"], "beta")

    def test_pbix_rollback_is_byte_identical(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        original = path.read_bytes()
        cid = editing.propose(str(path), '"alpha"', '"beta"', "why", member="Report/Layout")["change_id"]
        editing.finish(cid, "apply", self.codes[-1])
        self.approve(cid, "rollback")
        self.assertEqual(path.read_bytes(), original)

    def test_model_and_mashup_parts_are_never_editable(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        for member in ("DataModel", "DataMashup", "Version", "SecurityBindings", ""):
            with self.assertRaises(ValueError, msg=member):
                editing.propose(str(path), "a", "b", "why", member=member)
        with self.assertRaises(ValueError):
            editing.review(str(path), "DataModel")

    def test_pbit_model_schema_is_editable_but_a_pbix_one_is_not(self):
        pbit, pbix = self.root / "t.pbit", self.root / "t.pbix"
        make_pbix(pbit, with_schema=True)
        make_pbix(pbix, with_schema=True)
        cid = editing.propose(str(pbit), '"name": "m"', '"name": "m2"', "why", member="DataModelSchema")["change_id"]
        self.assertEqual(editing.finish(cid, "apply", self.codes[-1])["status"], "applied")
        with self.assertRaises(ValueError):
            editing.propose(str(pbix), '"name": "m"', '"name": "m2"', "why", member="DataModelSchema")

    def test_member_on_a_text_file_is_refused(self):
        path = self.put("m.tmdl", TMDL)
        with self.assertRaisesRegex(ValueError, "only applies"):
            editing.propose(str(path), "Total", "Sum", "why", member="Report/Layout")

    def test_not_a_zip_is_reported_clearly(self):
        path = self.root / "bad.pbix"
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "not a valid"):
            editing.propose(str(path), "a", "b", "why", member="Report/Layout")

    def test_failed_verification_restores_the_original(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        original = path.read_bytes()
        cid = editing.propose(str(path), '"alpha"', '"beta"', "why", member="Report/Layout")["change_id"]
        with mock.patch.object(store, "_verify", return_value=[{"check": "forced", "passed": False}]):
            with self.assertRaisesRegex(ValueError, "original was restored"):
                editing.finish(cid, "apply", self.codes[-1])
        self.assertEqual(path.read_bytes(), original)


class ReviewTests(EditCase):
    def test_review_lists_parts_and_flags_editable_ones(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        result = editing.review(str(path))
        flags = {p["name"]: p["text_editable"] for p in result["parts"]}
        self.assertTrue(flags["Report/Layout"])
        self.assertFalse(flags["DataModel"])
        self.assertTrue(result["editable_here"])

    def test_review_reads_a_layout_part_and_pages_through_it(self):
        path = self.root / "copy.pbix"
        make_pbix(path)
        first = editing.review(str(path), "Report/Layout", 0, 500)
        self.assertIs(first["well_formed"], True)
        self.assertIn("Page One", first["text"])
        long = self.put("big.tmdl", "table T\n" + "\tcolumn c\n" * 2000)
        page = editing.review(str(long), "", 0, 500)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["returned_chars"], 500)

    def test_review_redacts_credentials_in_rdl(self):
        path = self.put("r.rdl", RDL)
        shown = editing.review(str(path))["text"]
        self.assertNotIn("hunter2pass", shown)
        self.assertIn("[REDACTED]", shown)

    def test_review_works_outside_the_edit_folder_but_says_it_is_not_editable(self):
        original = self.originals / "orig.tmdl"
        original.write_text(TMDL)
        result = editing.review(str(original))
        self.assertFalse(result["editable_here"])

    def test_review_refuses_paths_outside_the_profile(self):
        with self.assertRaisesRegex(ValueError, "inside"):
            editing.review("C:\\Windows\\win.ini")

    def test_review_reports_malformed_files(self):
        path = self.put("bad.pbip", "{not json")
        self.assertIsInstance(editing.review(str(path))["well_formed"], str)

    def test_find_editable_files_skips_the_change_store(self):
        self.put("a.tmdl", TMDL)
        self.put("note.txt", "x")
        cid = editing.propose(str(self.root / "a.tmdl"), "Total", "Sum", "why")["change_id"]
        names = {Path(f["path"]).name for f in editing.find_editable_files()}
        self.assertEqual(names, {"a.tmdl"})
        self.assertTrue(cid)


class TextHelpersTests(unittest.TestCase):
    def test_decode_round_trips_each_encoding(self):
        for enc, raw in (("utf-8", b"abc"), ("utf-8-sig", b"\xef\xbb\xbfabc"), ("utf-16", b"\xff\xfea\x00b\x00c\x00"), ("utf-16-le", b"a\x00b\x00c\x00")):
            text, found = textedit.decode(raw)
            self.assertEqual(text, "abc")
            self.assertEqual(found, enc)
            self.assertEqual(textedit.encode(text, found), raw)

    def test_binary_is_not_decoded(self):
        with self.assertRaises(ValueError):
            textedit.decode(b"\xff\xfd\x80\x81\x82")

    def test_container_helpers_reject_missing_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.pbix"
            make_pbix(p)
            with self.assertRaises(ValueError):
                container.read_member(p, "Nope")


if __name__ == "__main__":
    unittest.main()
