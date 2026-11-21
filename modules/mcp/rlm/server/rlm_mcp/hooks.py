import json
import socket
from pathlib import Path

from . import child_log, harness, refine_run, session
from .config import Settings

CHILD_COMMANDS = ("/rlm-children", "/rlm children", "/rlm:rlm-children", "/rlm:rlm children")


def send_control(
    settings: Settings,
    target: int | session.SessionSource,
    msg: dict,
    timeout: float = 20.0,
) -> dict | None:
    if isinstance(target, session.SessionSource):
        path = target.control_socket_path(settings.data_dir)
    else:
        path = session.control_socket_path(settings.data_dir, target)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(msg) + "\n").encode())
            return json.loads(s.makefile("r").readline() or "null")
    except (OSError, ValueError):
        return None


def parse_child_command(text: str | None) -> tuple[bool, str | None]:
    # Match longest command prefix first so '/rlm children' matches before '/rlm'
    text = (text or "").strip()
    for cmd in sorted(CHILD_COMMANDS, key=len, reverse=True):
        if text == cmd:
            return True, None
        if text.startswith(cmd + " "):
            arg = text[len(cmd) :].strip()
            return True, arg or None
    return False, None


def format_child_report(
    settings: Settings, conversation_id: str | None, child_id: str | None = None
) -> str:
    if not conversation_id:
        return "no conversation id yet, so no child logs; it appears after the first turn"
    log_dir = settings.data_dir / "children" / conversation_id
    if child_id:
        return child_log.transcript(log_dir, child_id)
    return child_log.report(log_dir)


def build_harness_section(settings: Settings, conversation_id: str | None) -> tuple[str, str]:
    local = (
        harness.load(harness.local_path(settings.data_dir, conversation_id))
        if conversation_id
        else []
    )
    text = harness.build_section(harness.load(harness.global_path(settings.data_dir)), local)
    return text, harness.section_hash(text)


def on_session_start(
    source: session.SessionSource, fields: dict, settings: Settings
) -> str | None:
    sess = source.update_session(settings.data_dir, fields)
    text, digest = build_harness_section(settings, sess.get("conversationId"))
    source.update_session(settings.data_dir, {"harnessHash": digest})
    return text if text else None


def on_pre_invocation(
    source: session.SessionSource,
    fields: dict,
    settings: Settings,
    is_first_turn: bool = False,
) -> tuple[str | None, str | None]:
    sess = source.update_session(settings.data_dir, fields)
    conv = sess.get("conversationId")
    is_cmd, child_id = parse_child_command(fields.get("lastUserInput"))
    if is_first_turn and is_cmd:
        return format_child_report(settings, conv, child_id), None
    text, digest = build_harness_section(settings, conv)
    if not text or sess.get("harnessHash") == digest:
        return None, None
    source.update_session(settings.data_dir, {"harnessHash": digest})
    return None, text


def on_stop(source: session.SessionSource, fields: dict, settings: Settings) -> None:
    sess = source.update_session(settings.data_dir, fields)
    send_control(settings, source, {"type": "snapshot"})
    conv = sess.get("conversationId")
    if conv and refine_run.queue_path(settings.data_dir, conv).exists():
        refine_run.spawn_detached(settings, conv)


class AgyHookAdapter:
    SESSION_FIELDS = (
        "conversationId",
        "modelName",
        "transcriptPath",
        "workspacePaths",
        "lastUserInput",
    )

    @classmethod
    def extract_fields(cls, payload: dict) -> dict:
        return {k: payload[k] for k in cls.SESSION_FIELDS if payload.get(k) is not None}

    @classmethod
    def is_first_invocation(cls, payload: dict) -> bool:
        return payload.get("invocationNum") == 0

    @classmethod
    def child_report_response(cls, table: str) -> dict:
        return {
            "injectSteps": [{"errorMessage": {"userMessage": table}}],
            "terminationBehavior": "terminate",
        }

    @classmethod
    def system_message_response(cls, text: str) -> dict:
        # agy 1.5.6 drops the whole hook result if systemMessage carries any metadata.
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}

    @classmethod
    def empty_response(cls) -> dict:
        return {}


def handle(
    event: str,
    payload: dict,
    settings: Settings,
    agy_pid: int | None = None,
    session_source: session.SessionSource | None = None,
) -> dict:
    source = session_source or session.get_session_source(agy_pid)
    adapter = AgyHookAdapter
    fields = adapter.extract_fields(payload)

    if event == "SessionStart":
        text = on_session_start(source, fields, settings)
        if text:
            return adapter.system_message_response(text)
        return adapter.empty_response()

    if event == "PreInvocation":
        child_report, text = on_pre_invocation(
            source, fields, settings, is_first_turn=adapter.is_first_invocation(payload)
        )
        if child_report is not None:
            return adapter.child_report_response(child_report)
        if text is not None:
            return adapter.system_message_response(text)
        return adapter.empty_response()

    if event == "Stop":
        on_stop(source, fields, settings)
        return adapter.empty_response()

    return adapter.empty_response()
