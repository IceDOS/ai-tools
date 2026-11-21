import json
import os
import pytest
from rlm_mcp import children, runner, session
from rlm_mcp.config import Settings
from rlm_mcp.hooks import AgyHookAdapter


def test_get_runner():
    r = runner.get_runner("agy")
    assert isinstance(r, runner.AgyRunner)
    assert r.name == "agy"

    op = runner.get_runner("opencode")
    assert isinstance(op, runner.OpencodeRunner)
    assert op.name == "opencode"

    with pytest.raises(ValueError, match="unknown runner backend"):
        runner.get_runner("nonexistent")


def test_agy_session_source(tmp_path):
    src = session.get_session_source(pid=123)
    assert src.session_key == "123"
    assert isinstance(src, session.AgySessionSource)
    assert src.control_socket_path(tmp_path).name == "123.sock"


def test_opencode_session_source(tmp_path):
    src = session.get_session_source(key="ses_abc123", runner="opencode")
    assert isinstance(src, session.OpencodeSessionSource)
    assert src.session_key == "ses_abc123"
    assert src.conversation_id(tmp_path) == "ses_abc123"
    assert src.control_socket_path(tmp_path).name == "ses_abc123.sock"

    # Reading non-existent session yields fallback dict
    s = src.read_session(tmp_path)
    assert s["conversationId"] == "ses_abc123"

    # Updating session writes file
    src.update_session(tmp_path, {"modelName": "test-model"})
    s2 = src.read_session(tmp_path)
    assert s2["modelName"] == "test-model"


def test_opencode_runner_argv(tmp_path):
    settings = Settings.from_env({
        "RLM_OPENCODE": "/bin/opencode",
        "PLUGIN_DATA": str(tmp_path),
    })
    r = runner.OpencodeRunner()
    argv = r.build_child_argv(settings, "hello", model="gpt-4o")
    assert argv[:6] == ["/bin/opencode", "run", "--format", "json", "--agent", "rlm"]
    assert "--auto" in argv
    assert "--standalone" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "gpt-4o"
    assert argv[-2:] == ["--", "hello"]

    argv_nomodel = r.build_child_argv(settings, "hello", model=None)
    assert "--model" not in argv_nomodel


def test_opencode_runner_parse_stream():
    r = runner.OpencodeRunner()

    # Normal multi-step response with tokens
    lines = [
        json.dumps({"type": "step_start", "part": {"id": "s1"}}),
        json.dumps({"type": "text", "part": {"text": "hello "}}),
        json.dumps({"type": "text", "part": {"text": "world"}}),
        json.dumps({
            "type": "step_finish",
            "part": {"tokens": {"input": 10, "output": 2}},
        }),
    ]
    res = r.parse_child_stream(lines)
    assert res.ok
    assert res.output == "hello world"
    assert res.turns == 1
    assert res.usage == {"input": 10, "output": 2}

    # Error event
    lines_err = [
        json.dumps({"type": "step_start", "part": {"id": "s1"}}),
        json.dumps({"type": "error", "error": {"message": "provider quota exceeded"}}),
    ]
    res_err = r.parse_child_stream(lines_err)
    assert not res_err.ok
    assert "provider quota exceeded" in res_err.error

    # Empty stream
    res_empty = r.parse_child_stream([])
    assert not res_empty.ok
    assert "without any output" in res_empty.error


def test_opencode_runner_refine():
    r = runner.OpencodeRunner()
    settings = Settings.from_env()
    argv = r.build_refine_argv(settings, "optimize", {"type": "object"}, model="claude-3")
    assert "run" in argv and "--format" in argv and "json" in argv
    assert "--auto" in argv and "--standalone" in argv
    assert argv[argv.index("--model") + 1] == "claude-3"

    # Parse clean JSON
    proposal, err = r.parse_refine_output('{"summary": "clean", "edits": []}', "")
    assert err is None
    assert proposal == {"summary": "clean", "edits": []}

    # Parse markdown fenced JSON
    fenced = "Here is the result:\n```json\n{\"summary\": \"fenced\", \"edits\": []}\n```"
    proposal2, err2 = r.parse_refine_output(fenced, "")
    assert err2 is None
    assert proposal2 == {"summary": "fenced", "edits": []}

    # Parse stream json lines with text part
    stream_output = "\n".join([
        json.dumps({"type": "step_start"}),
        json.dumps({"type": "text", "part": {"text": '{"summary": "from stream", "edits": []}'}}),
        json.dumps({"type": "step_finish"}),
    ])
    proposal3, err3 = r.parse_refine_output(stream_output, "")
    assert err3 is None
    assert proposal3 == {"summary": "from stream", "edits": []}


def test_opencode_run_child_echo(opencode_settings, tmp_path, monkeypatch):
    rec = tmp_path / "rec.jsonl"
    monkeypatch.setenv("FAKE_OPENCODE_RECORD", str(rec))
    log_dir = tmp_path / "logs"

    res = children.run_child(opencode_settings, "test prompt", 1, "test-model", log_dir, "parent-123")
    assert res.ok
    assert res.output.startswith("echo[1]:")
    assert res.turns == 1
    assert res.usage == {"input": 10, "output": 5}

    records = [json.loads(line) for line in rec.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["depth"] == "1"
    assert records[0]["parent_id"] == "parent-123"
    assert "--standalone" in records[0]["argv"]
    assert "--auto" in records[0]["argv"]


def test_opencode_run_child_timeout_kill(opencode_settings, monkeypatch):
    # Set fake opencode to sleep and short child timeout
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "sleep")
    short_timeout_settings = Settings.from_env({
        "RLM_OPENCODE": opencode_settings.opencode,
        "RLM_RUNNER": "opencode",
        "RLM_CHILD_TIMEOUT": "0.3s",
        "PLUGIN_DATA": str(opencode_settings.data_dir),
    })

    res = children.run_child(short_timeout_settings, "will time out", 1, None, None, None)
    assert not res.ok
    assert "timed out after 0.3s" in res.error


def test_agy_hook_adapter():
    payload = {"conversationId": "c1", "other": "ignored", "invocationNum": 0}
    fields = AgyHookAdapter.extract_fields(payload)
    assert fields == {"conversationId": "c1"}
    assert AgyHookAdapter.is_first_invocation(payload) is True
    assert AgyHookAdapter.is_first_invocation({"invocationNum": 1}) is False
    assert "errorMessage" in AgyHookAdapter.child_report_response("tab")["injectSteps"][0]
    assert "systemMessage" in AgyHookAdapter.system_message_response("msg")["injectSteps"][0]
