"""Tests for the local learning loop (pbi_mcp.plus.learning)."""
import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pbi_mcp.plus import learning  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PBI_MCP_LEARN_DIR", str(tmp_path / "learn"))
    monkeypatch.delenv("PBI_MCP_LEARN", raising=False)
    learning._last_failure.clear()


def test_redaction_removes_paths_names_secrets_and_ids():
    msg = r"Cannot open C:\Users\jane\Docs\Budget 2026.pbip for 'Student Scores' [Total Sales] password=hunter2 a@b.org 123e4567-e89b-12d3-a456-426614174000"
    out = learning.redact(msg)
    for leak in ("jane", "Budget", "Student", "Total Sales", "hunter2", "a@b.org", "123e4567"):
        assert leak not in out
    assert learning.signature("t", "row 12 failed") == learning.signature("t", "row 99 failed")


def test_observe_logs_only_redacted_pattern_and_tracks_retry_resolution():
    learning.observe("plan_model_change", False, 12, r"No such table 'Patients' in D:\secret\x.pbip")
    learning.observe("plan_model_change", True, 8)
    rows = [json.loads(l) for l in (learning.learn_dir() / "events.jsonl").read_text().splitlines()]
    assert "Patients" not in json.dumps(rows) and "secret" not in json.dumps(rows)
    assert rows[1]["resolved_sig"] == rows[0]["sig"]


def test_switch_off_writes_nothing(monkeypatch):
    monkeypatch.setenv("PBI_MCP_LEARN", "0")
    learning.observe("x", True, 1)
    assert not learning.learn_dir().exists()
    with pytest.raises(learning.LearnError):
        learning.record_lesson("x", "a problem here", "a fix here ok")


def test_lessons_roundtrip_dedupe_and_hint():
    r = learning.record_lesson("apply_change", "plan expired because the file changed after planning", "Plan again after the user saves in Desktop")
    assert r["status"] == "recorded"
    assert learning.record_lesson("apply_change", "plan expired because the file changed after planning", "Plan again after the user saves in Desktop")["confirmed"] == 2
    assert learning.get_lessons("apply_change", "plan expired")[0]["confirmed"] == 2
    assert "Plan again" in learning.hint_for("apply_change", "Plan expired: the file changed after planning")
    assert learning.hint_for("apply_change", "completely unrelated message here") is None
    with pytest.raises(learning.LearnError):
        learning.record_lesson("bad tool!", "a problem here", "a fix here ok")


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_knowledge_sync_search_and_safety(monkeypatch):
    blob = _zip({"kb-master/README.md": "# Intro\nwelcome", "kb-master/dax/time-intelligence.md": "# DATEADD tips\nUse DATEADD with a marked date table for time intelligence.",
                 "kb-master/../evil.md": "x", "kb-master/run.exe": "MZ", "kb-master/notes/big.md": "# a\n" + "x" * 500000})

    def fake(url, limit):
        return json.dumps({"default_branch": "master", "pushed_at": "2026-01-01"}).encode() if "api.github.com" in url else blob

    monkeypatch.setattr(learning, "_http", fake)
    with pytest.raises(learning.LearnError, match="not allowed"):
        learning.sync_knowledge_base("someone/else")
    res = learning.sync_knowledge_base()
    assert res["files"] == 2 and res["skipped"] >= 2 and not res["empty"]
    assert not (learning.learn_dir() / "kb" / "evil.md").exists()
    hit = learning.search_knowledge("DATEADD time intelligence")[0]
    assert hit["file"] == "dax/time-intelligence.md" and hit["untrusted"]
    assert learning.kb_status()[0]["files"] == 2 and learning.kb_status()[0]["age_days"] >= 0


def test_empty_knowledge_base_is_reported_not_fatal(monkeypatch):
    monkeypatch.setattr(learning, "_http", lambda url, limit: json.dumps({"default_branch": "master"}).encode() if "api." in url else _zip({"x-master/": ""}))
    assert learning.sync_knowledge_base()["empty"]
    assert any("no readable files" in s for s in learning.improvement_report()["suggestions"])


def test_report_flags_repeated_unresolved_errors_and_forget_previews():
    for _ in range(4):
        learning.observe("lint_dax", False, 5, "Unbalanced brackets in expression")
    rep = learning.improvement_report()
    assert rep["total_errors"] == 4 and any("lint_dax" in s for s in rep["suggestions"]) and rep["draft_issue"]
    assert learning.forget()["status"] == "preview" and learning.learn_dir().exists()
    assert learning.forget(confirm=True)["status"] == "deleted" and not learning.learn_dir().exists()


def test_server_hook_records_real_tool_calls():
    from pbi_mcp import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"get_lessons", "record_lesson", "sync_knowledge_base", "search_knowledge", "improvement_report", "forget_learning", "learning_status"} <= names
    asyncio.run(server.mcp.call_tool("learning_status", {}))
    with pytest.raises(Exception):
        asyncio.run(server.mcp.call_tool("search_knowledge", {"query": "a"}))
    rows = [json.loads(l) for l in (learning.learn_dir() / "events.jsonl").read_text().splitlines()]
    assert [(r["tool"], r["ok"]) for r in rows] == [("learning_status", True), ("search_knowledge", False)]
