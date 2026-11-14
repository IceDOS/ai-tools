import json
import socket

from . import harness, refine_run, session
from .config import Settings

SESSION_FIELDS = (
    "conversationId",
    "modelName",
    "transcriptPath",
    "workspacePaths",
    "lastUserInput",
)


def send_control(
    settings: Settings, agy_pid: int, msg: dict, timeout: float = 20.0
) -> dict | None:
    path = session.control_socket_path(settings.data_dir, agy_pid)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(msg) + "\n").encode())
            return json.loads(s.makefile("r").readline() or "null")
    except (OSError, ValueError):
        return None


def _section(settings: Settings, conversation_id: str | None) -> str:
    local = (
        harness.load(harness.local_path(settings.data_dir, conversation_id))
        if conversation_id
        else []
    )
    return harness.build_section(harness.load(harness.global_path(settings.data_dir)), local)


def handle(event: str, payload: dict, settings: Settings, agy_pid: int) -> dict:
    fields = {k: payload[k] for k in SESSION_FIELDS if payload.get(k) is not None}
    if event in ("SessionStart", "PreInvocation"):
        sess = session.update_session(settings.data_dir, agy_pid, fields)
        text = _section(settings, sess.get("conversationId"))
        digest = harness.section_hash(text)
        if not text or (event == "PreInvocation" and sess.get("harnessHash") == digest):
            if event == "SessionStart":
                session.update_session(settings.data_dir, agy_pid, {"harnessHash": digest})
            return {}
        session.update_session(settings.data_dir, agy_pid, {"harnessHash": digest})
        return {"injectSteps": [{"systemMessage": text}]}
    if event == "Stop":
        sess = session.update_session(settings.data_dir, agy_pid, fields)
        send_control(settings, agy_pid, {"type": "snapshot"})
        conv = sess.get("conversationId")
        if conv and refine_run.queue_path(settings.data_dir, conv).exists():
            refine_run.spawn_detached(settings, conv)
        return {}
    return {}
