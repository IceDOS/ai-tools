import json
import os

from rlm_mcp import children
from rlm_mcp.config import Settings


def test_child_prompt_small_and_large(tmp_path):
    p = children.child_prompt("do x", 1, 2, None)
    assert "depth 1" in p and "maximum 2" in p and p.endswith("do x")
    big = children.child_prompt("A" * 150_000, 2, 2, tmp_path / "t.txt")
    assert "variable `task`" in big and "150000 chars" in big and len(big) < 10_000


def test_build_argv(settings):
    argv = children.build_argv(settings, "hello", "gemini-pro")
    assert argv[:3] == [settings.agy, "-p", "hello"]
    for flag in [
        "--agent",
        "rlm",
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "1m",
        "--model",
        "gemini-pro",
    ]:
        assert flag in argv
    assert "--model" not in children.build_argv(settings, "hello", None)


def test_parse_stream_success_and_missing_result():
    lines = [
        json.dumps({"type": "init"}),
        json.dumps({
            "type": "result",
            "status": "SUCCESS",
            "response": "hi",
            "usage": {"a": 1},
            "num_turns": 3,
        }),
    ]
    r = children.parse_stream(lines)
    assert r.ok and r.output == "hi" and r.turns == 3 and r.usage == {"a": 1}
    r = children.parse_stream([json.dumps({"type": "init"}), "not json"])
    assert not r.ok and "without a result" in r.error


def test_parse_stream_event_result():
    lines = [
        json.dumps({"event": "init"}),
        json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "hi from event",
                "usage": {"a": 2},
                "num_turns": 1,
            },
        }),
    ]
    r = children.parse_stream(lines)
    assert r.ok and r.output == "hi from event" and r.turns == 1 and r.usage == {"a": 2}


def test_parse_stream_error_status():
    r = children.parse_stream([json.dumps({"type": "result", "status": "ERROR", "error": "bad"})])
    assert not r.ok and r.error == "bad"


def test_add_usage_nested():
    total = {}
    children.add_usage(total, {"in": 1, "cost": {"total": 0.5}})
    children.add_usage(total, {"in": 2, "cost": {"total": 0.25}})
    assert total == {"in": 3, "cost": {"total": 0.75}}


def test_run_child_echo_and_log(settings, tmp_path, monkeypatch):
    record = tmp_path / "rec.jsonl"
    monkeypatch.setenv("FAKE_AGY_RECORD", str(record))
    log_dir = tmp_path / "logs"
    r = children.run_child(settings, "summarize", 1, "m1", log_dir, "parent-1")
    assert r.ok and r.output.startswith("echo[1]:")
    rec = json.loads(record.read_text().splitlines()[0])
    assert rec["depth"] == "1" and rec["parent_id"] == "parent-1" and rec["log_dir"] == str(log_dir)
    log = next(log_dir.glob("*.jsonl")).read_text().splitlines()
    assert json.loads(log[0])["type"] == "rlm_child_start"
    assert json.loads(log[-1])["type"] == "rlm_child_end"


def test_run_child_large_prompt_uses_task_file(settings, tmp_path, monkeypatch):
    record = tmp_path / "rec.jsonl"
    monkeypatch.setenv("FAKE_AGY_RECORD", str(record))
    r = children.run_child(settings, "B" * 150_000, 1, None, None, None)
    assert r.ok and r.output.endswith("task=150000")
    rec = json.loads(record.read_text().splitlines()[0])
    assert rec["task_file"] and not os.path.exists(rec["task_file"])


def test_run_child_failure(settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "fail")
    r = children.run_child(settings, "x", 1, None, None, None)
    assert not r.ok and "boom" in r.error


def test_run_child_cwd(settings, tmp_path, monkeypatch):
    record = tmp_path / "rec.jsonl"
    monkeypatch.setenv("FAKE_AGY_RECORD", str(record))
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    r = children.run_child(settings, "summarize", 1, "m1", None, None, cwd=workdir)
    assert r.ok
    rec = json.loads(record.read_text().splitlines()[0])
    assert rec["cwd"] == str(workdir)
