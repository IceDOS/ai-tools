import json
import os
import time
from pathlib import Path

from rlm_mcp import session


def test_update_and_read_by_pid(tmp_path):
    session.update_session(tmp_path, 42, {"conversationId": "c1", "modelName": "m"})
    merged = session.update_session(tmp_path, 42, {"lastUserInput": "hi"})
    assert merged == {"conversationId": "c1", "modelName": "m", "lastUserInput": "hi"}
    assert session.read_session(tmp_path, 42) == merged


def test_read_falls_back_to_workspace(tmp_path):
    session.update_session(tmp_path, 7, {"conversationId": "c7", "workspacePaths": ["/w"]})
    assert session.read_session(tmp_path, 99, cwd="/w")["conversationId"] == "c7"
    assert session.read_session(tmp_path, 99, cwd="/other") is None


def test_stale_workspace_match_ignored(tmp_path):
    path = session.update_session(tmp_path, 7, {"conversationId": "c7", "workspacePaths": ["/w"]})
    old = time.time() - 3600
    os.utime(session.session_path(tmp_path, 7), (old, old))
    assert session.read_session(tmp_path, 99, cwd="/w") is None


def test_env_override(monkeypatch):
    monkeypatch.setenv("RLM_AGY_PID", "1234")
    assert session.find_agy_pid() == 1234


def test_find_agy_pid_falls_back_to_parent(monkeypatch):
    monkeypatch.delenv("RLM_AGY_PID", raising=False)
    monkeypatch.setattr(session, "_comm_and_parent", lambda pid: ("bash", 1))
    assert session.find_agy_pid() == os.getppid()


def test_paths(tmp_path):
    assert session.session_path(tmp_path, 5) == tmp_path / "sessions" / "5.json"
    assert session.control_socket_path(tmp_path, 5) == tmp_path / "run" / "5.sock"
    long_path = Path("/a" * 60)
    sock = session.control_socket_path(long_path, 5)
    assert sock.name == "5.sock" and len(str(sock).encode("utf-8")) < 104


def test_resolve_workspace_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "readlink", lambda p: str(tmp_path))
    assert session.resolve_workspace(1234) == str(tmp_path)


def test_resolve_workspace_fallback_on_error(monkeypatch):
    def fail(p):
        raise OSError("process does not exist")

    monkeypatch.setattr(os, "readlink", fail)
    assert session.resolve_workspace(999999) == os.getcwd()
