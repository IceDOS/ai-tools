import json

from rlm_mcp import harness, refine_run


def test_queue_and_pop(tmp_path):
    assert refine_run.queue_refine(tmp_path, None, "x", False) == (
        False,
        "no conversation id yet; try again after the first turn",
    )
    assert refine_run.queue_refine(tmp_path, "c1", "be terse", True) == (True, None)
    refine_run.queue_refine(tmp_path, "c1", None, False)
    assert refine_run.pop_queue(tmp_path, "c1") == [
        {"instructions": "be terse", "global": True},
        {"instructions": None, "global": False},
    ]
    assert refine_run.pop_queue(tmp_path, "c1") == []


def test_transcript_tail(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("a" * 30_000 + "END")
    tail = refine_run.transcript_tail(str(f))
    assert len(tail) == 20_000 and tail.endswith("END")
    assert refine_run.transcript_tail(None) == ""


def test_run_refinement_applies_structured_output(settings, monkeypatch, tmp_path):
    structured = {
        "summary": "added",
        "edits": [{"action": "create", "title": "T", "content": "C"}],
    }
    monkeypatch.setenv("FAKE_AGY_STRUCTURED", json.dumps(structured))
    out = refine_run.run_refinement(
        settings, "c9", None, {"instructions": None, "global": True}, None
    )
    assert out["summary"] == "added" and out["errors"] == []
    assert harness.load(harness.global_path(settings.data_dir))[0]["title"] == "T"


def test_run_refinement_child_failure(settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "fail")
    out = refine_run.run_refinement(
        settings, "c9", None, {"instructions": None, "global": False}, None
    )
    assert out["applied"] == [] and out["errors"]
