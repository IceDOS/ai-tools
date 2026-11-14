import json
import threading

from rlm_mcp import session
from rlm_mcp.service import RlmService


def svc(settings, pid=4242, **fields):
    session.update_session(
        settings.data_dir,
        pid,
        {
            "conversationId": "conv1",
            "modelName": "m1",
            "lastUserInput": "the question",
            **fields,
        },
    )
    return RlmService(settings, pid)


def test_ipython_sets_user_prompt_and_runs(settings):
    s = svc(settings)
    try:
        assert s.ipython("user_prompt") == "Out: 'the question'"
    finally:
        s.shutdown()


def test_rlm_child_and_children_line(settings):
    s = svc(settings)
    try:
        out = s.ipython("r = rlm('hello')\nr[:8]")
        assert "Out: 'echo[1]:'" in out and "[rlm children: 1" in out
        assert len(list((settings.data_dir / "children" / "conv1").glob("*.jsonl"))) == 1
        assert "conv1" in s.children()
    finally:
        s.shutdown()


def test_depth_limit(settings):
    s = svc(settings)
    try:
        s.set_depth(0)
        out = s.ipython("rlm('x')")
        assert "maximum recursion depth 0" in out
    finally:
        s.shutdown()


def test_refine_queue_from_kernel(settings):
    s = svc(settings)
    try:
        assert s.ipython("refine('be terse')") == "Out: {'scheduled': True}"
        assert (settings.data_dir / "refine-queue" / "conv1.json").exists()
    finally:
        s.shutdown()


def test_snapshot_via_control_socket(settings):
    s = svc(settings)
    s.serve_control()
    try:
        s.ipython("keep = {'a': 1}")
        from rlm_mcp.hooks import send_control

        assert send_control(settings, 4242, {"type": "snapshot"}) == {"ok": True}
        assert (settings.data_dir / "snapshots" / "conv1.pickle").exists()
    finally:
        s.shutdown()
    again = svc(settings)
    try:
        assert again.ipython("keep") == "Out: {'a': 1}"
    finally:
        again.shutdown()


def test_task_file_preload(settings, tmp_path, monkeypatch):
    task = tmp_path / "task.txt"
    task.write_text("Z" * 1234)
    monkeypatch.setenv("RLM_TASK_FILE", str(task))
    s = svc(settings)
    try:
        assert s.ipython("len(task)") == "Out: 1234"
    finally:
        s.shutdown()


def test_status_and_restart(settings):
    s = svc(settings)
    try:
        s.ipython("z = 1")
        assert "conversation: conv1" in s.status() and "depth: 0 of 2" in s.status()
        assert "restarted" in s.restart()
        assert "NameError" in s.ipython("z")
    finally:
        s.shutdown()


def test_service_cwd_passed_to_session_and_host(settings, tmp_path):
    session.update_session(settings.data_dir, 1001, {"conversationId": "c1001", "workspacePaths": [str(tmp_path)]})
    s = RlmService(settings, 9999, cwd=str(tmp_path))
    assert s.cwd == str(tmp_path)
    assert s.session().get("conversationId") == "c1001"
    assert s._host().cwd == str(tmp_path)


def test_service_ipython_with_image(settings, tmp_path):
    img_file = tmp_path / "plot.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    s = svc(settings)
    try:
        out = s.ipython(f"show_image({str(img_file)!r})")
        assert isinstance(out, str)
        assert "<Image: image/png, 28 bytes>" in out
        assert len(out.images) == 1
        assert out.images[0]["mime"] == "image/png"
    finally:
        s.shutdown()

