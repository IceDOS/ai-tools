import json
import os
import subprocess
import time
from pathlib import Path

from rlm_mcp import child_log

NOW = 1090.0


def write(log_dir: Path, cid: str, records: list[dict], mtime: float | None = None) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{cid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def start(cid="c1", parent=None, depth=1, prompt="do the thing", model="m1", pid=1, ts=1000.0):
    return {
        "type": "rlm_child_start",
        "id": cid,
        "parent": parent,
        "depth": depth,
        "model": model,
        "pid": pid,
        "ts": ts,
        "prompt": prompt,
    }


def end(cid="c1", ok=True, error=None, turns=2, usage=None, ts=1010.0):
    return {
        "type": "rlm_child_end",
        "id": cid,
        "ok": ok,
        "error": error,
        "status": "SUCCESS" if ok else "ERROR",
        "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5},
        "turns": turns,
        "ts": ts,
    }


def test_read_ended_child(tmp_path):
    write(tmp_path, "c1", [
        start(parent="p0", depth=2),
        {"type": "step_update", "step_type": "agent_response"},
        {"type": "result", "status": "SUCCESS", "num_turns": 2},
        end(),
    ])
    [child] = child_log.read(tmp_path)
    assert child.id == "c1" and child.parent == "p0" and child.depth == 2
    assert child.model == "m1" and child.prompt == "do the thing" and child.pid == 1
    assert child.state() == "ok" and child.ok is True
    assert child.turns == 2 and child.tokens == 15 and child.steps == 1
    assert child.started == 1000.0 and child.ended == 1010.0


def test_read_running_child_counts_steps_and_activity(tmp_path):
    write(tmp_path, "c1", [
        start(pid=os.getpid(), ts=time.time()),
        {"type": "init"},
        {"type": "step_update"},
        {"type": "step_update"},
    ])
    [child] = child_log.read(tmp_path)
    assert child.state() == "running" and child.steps == 2 and child.activity == "step_update"
    assert child.turns == 0 and child.tokens == 0


def test_read_lost_child_when_spawner_is_gone(tmp_path):
    dead = subprocess.Popen(["true"])
    dead.wait()
    write(tmp_path, "c1", [start(pid=dead.pid)])
    [child] = child_log.read(tmp_path)
    assert child.state() == "lost" and child.lost and child.ended is not None


def test_alive_rejects_zombie():
    zombie = subprocess.Popen(["true"])
    try:
        for _ in range(100):
            stat = Path(f"/proc/{zombie.pid}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.02)
        assert not child_log._alive(zombie.pid)
    finally:
        zombie.wait()


def test_tokens_prefers_total_and_ignores_cache():
    assert child_log._tokens({"total_tokens": 42, "input_tokens": 10}) == 42
    assert child_log._tokens({"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 99}) == 15
    assert child_log._tokens({"input_tokens": 10, "nested": {"output_tokens": 5}}) == 10
    assert child_log._tokens(None) == 0


def test_read_orders_by_start_and_numbers_rows(tmp_path):
    write(tmp_path, "late", [start(cid="late", ts=2000.0), end(cid="late", ts=2010.0)])
    write(tmp_path, "early", [start(cid="early", ts=1000.0), end(cid="early", ts=1010.0)])
    assert [(c.seq, c.id) for c in child_log.read(tmp_path)] == [(1, "early"), (2, "late")]


def test_report_table(tmp_path):
    write(tmp_path, "aaa", [
        start(cid="aaa", prompt="summarize\nthe   logs", ts=1000.0),
        {"type": "step_update"},
        {"type": "step_update"},
        end(cid="aaa", usage={"total_tokens": 1500}, turns=3, ts=1010.0),
    ], mtime=1010.0)
    write(tmp_path, "bbb", [start(cid="bbb", parent="aaa", depth=2, model=None, prompt="x" * 100, ts=1010.0, pid=os.getpid())], mtime=1010.0)
    write(tmp_path, "ccc", [start(cid="ccc", prompt="fail please", ts=1010.0), end(cid="ccc", ok=False, error="child exited with code 3", ts=1012.0)], mtime=1012.0)
    lines = child_log.report(tmp_path, now=NOW).splitlines()
    assert lines[0] == f"children in {tmp_path}: 3 total, 1 running"
    assert lines[1].split('"')[0].split() == ["#1", "ok", "d1", "0:10", "3t", "1.5k", "tok", "m1", "-"]
    assert '"summarize the logs"' in lines[1] and lines[1].endswith("aaa.jsonl")
    assert "#2" in lines[2] and "running" in lines[2] and "d2" in lines[2] and "default" in lines[2]
    assert "#1" in lines[2] and "▸ starting" in lines[2] and "(idle 1:20)" in lines[2]
    assert lines[3].split('"')[0].split()[1:] == ["failed", "d1", "0:02", "2t", "15", "tok", "m1", "-"]
    assert '"fail please"' in lines[3] and lines[3].endswith("ccc.jsonl")
    assert lines[4] == "    error: child exited with code 3"


def test_report_truncates_prompt_to_one_line(tmp_path):
    write(tmp_path, "aaa", [start(prompt="a\nb" * 80, pid=os.getpid(), ts=1000.0)], mtime=1000.0)
    row = child_log.report(tmp_path, now=NOW).splitlines()[1]
    assert "\n" not in row and '…"' in row and len(row) < 200


def test_report_empty_and_missing_dir(tmp_path):
    assert child_log.report(tmp_path) == f"children in {tmp_path}: none logged"
    assert child_log.read(tmp_path) == []


def test_read_skips_partial_trailing_line(tmp_path):
    (tmp_path / "zzz.jsonl").write_text('{"type": "rlm_child_start", "id": "zzz", "pid": 1}\n{"type": "res')
    [child] = child_log.read(tmp_path)
    assert child.id == "zzz" and child.state() == "running" and child.activity == ""
