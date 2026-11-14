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
    assert "Continual harness" in out["injectSteps"][0]["systemMessage"]


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
    assert "new" in out["injectSteps"][0]["systemMessage"]
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
