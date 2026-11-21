import json

from rlm_mcp import harness, hooks, refine_run, session


def payload(**extra):
    return {
        "conversationId": "conv1",
        "modelName": "m1",
        "transcriptPath": "/t",
        "workspacePaths": ["/w"],
        "lastUserInput": "hi",
        **extra,
    }


def test_session_start_writes_session_and_injects(settings):
    harness.apply_edits(
        [{"action": "create", "scope": "global", "title": "T", "content": "C"}],
        "global",
        harness.global_path(settings.data_dir),
        harness.local_path(settings.data_dir, "conv1"),
    )
    out = hooks.handle("SessionStart", payload(), settings, 77)
    assert session.read_session(settings.data_dir, 77)["conversationId"] == "conv1"
    assert "Continual harness" in out["injectSteps"][0]["systemMessage"]["systemMessage"]


def test_session_start_without_entries_injects_nothing(settings):
    assert hooks.handle("SessionStart", payload(), settings, 77) == {}


def test_pre_invocation_updates_and_reinjects_only_on_change(settings):
    hooks.handle("SessionStart", payload(), settings, 77)
    assert hooks.handle("PreInvocation", payload(lastUserInput="next"), settings, 77) == {}
    assert session.read_session(settings.data_dir, 77)["lastUserInput"] == "next"
    harness.apply_edits(
        [{"action": "create", "title": "N", "content": "new"}],
        "local",
        harness.global_path(settings.data_dir),
        harness.local_path(settings.data_dir, "conv1"),
    )
    out = hooks.handle("PreInvocation", payload(), settings, 77)
    assert "new" in out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert hooks.handle("PreInvocation", payload(), settings, 77) == {}


def test_stop_spawns_refine_when_queued(settings, monkeypatch):
    spawned = []
    monkeypatch.setattr(refine_run, "spawn_detached", lambda s, c: spawned.append(c))
    hooks.handle("SessionStart", payload(), settings, 77)
    assert hooks.handle("Stop", payload(), settings, 77) == {}
    assert spawned == []
    refine_run.queue_refine(settings.data_dir, "conv1", "x", False)
    hooks.handle("Stop", payload(), settings, 77)
    assert spawned == ["conv1"]

def child_log_dir(settings):
    return settings.data_dir / "children" / "conv1"


def write_child(settings, cid="c1", prompt="hello", ok=True):
    log_dir = child_log_dir(settings)
    log_dir.mkdir(parents=True, exist_ok=True)
    start = {"type": "rlm_child_start", "id": cid, "parent": None, "depth": 1, "model": "m1",
             "pid": 1234, "ts": 1000.0, "prompt": prompt}
    end = {"type": "rlm_child_end", "id": cid, "ok": ok, "error": None, "status": "SUCCESS",
           "usage": {"total_tokens": 15}, "turns": 2, "ts": 1010.0}
    (log_dir / f"{cid}.jsonl").write_text(json.dumps(start) + "\n" + json.dumps(end) + "\n")


def test_pre_invocation_answers_rlm_children_without_the_model(settings):
    write_child(settings)
    out = hooks.handle(
        "PreInvocation", payload(lastUserInput="/rlm-children", invocationNum=0), settings, 77
    )
    assert out["terminationBehavior"] == "terminate"
    table = out["injectSteps"][0]["errorMessage"]["userMessage"]
    assert table.startswith(f"children in {child_log_dir(settings)}: 1 total, 0 running")
    assert '"hello"' in table and "15 tok" in table
    assert session.read_session(settings.data_dir, 77)["lastUserInput"] == "/rlm-children"


def test_pre_invocation_rlm_children_alias_and_whitespace(settings):
    write_child(settings)
    out = hooks.handle(
        "PreInvocation", payload(lastUserInput="  /rlm children\n", invocationNum=0), settings, 77
    )
    assert out["terminationBehavior"] == "terminate"
    assert "1 total" in out["injectSteps"][0]["errorMessage"]["userMessage"]


def test_pre_invocation_rlm_children_needs_invocation_zero(settings):
    write_child(settings)
    out = hooks.handle(
        "PreInvocation", payload(lastUserInput="/rlm-children", invocationNum=1), settings, 77
    )
    assert out == {}


def test_pre_invocation_rlm_children_namespaced_forms(settings):
    write_child(settings)
    for command in ("/rlm:rlm-children", "/rlm:rlm-children please", "/rlm:rlm children"):
        out = hooks.handle(
            "PreInvocation", payload(lastUserInput=command, invocationNum=0), settings, 77
        )
        assert out["terminationBehavior"] == "terminate", command
        assert "1 total" in out["injectSteps"][0]["errorMessage"]["userMessage"], command


def test_pre_invocation_other_input_still_passes_through(settings):
    write_child(settings)
    assert hooks.handle("PreInvocation", payload(invocationNum=0), settings, 77) == {}
    for text in ("/rlm status", "/rlm-childrenx"):
        assert hooks.handle("PreInvocation", payload(lastUserInput=text, invocationNum=0), settings, 77) == {}


def test_pre_invocation_rlm_children_without_conversation(settings):
    out = hooks.handle(
        "PreInvocation",
        {"lastUserInput": "/rlm-children", "invocationNum": 0},
        settings,
        77,
    )
    assert out["terminationBehavior"] == "terminate"
    assert "no conversation id" in out["injectSteps"][0]["errorMessage"]["userMessage"]


def test_pre_invocation_rlm_children_skips_the_harness_section(settings):
    hooks.handle("SessionStart", payload(), settings, 77)
    harness.apply_edits(
        [{"action": "create", "title": "N", "content": "new"}],
        "local",
        harness.global_path(settings.data_dir),
        harness.local_path(settings.data_dir, "conv1"),
    )
    out = hooks.handle(
        "PreInvocation", payload(lastUserInput="/rlm-children", invocationNum=0), settings, 77
    )
    assert list(out["injectSteps"][0]) == ["errorMessage"] and "Continual harness" not in str(out)
    # The harness is still pending for the next real invocation.
    nxt = hooks.handle("PreInvocation", payload(lastUserInput="next question"), settings, 77)
    assert "new" in nxt["injectSteps"][0]["systemMessage"]["systemMessage"]
