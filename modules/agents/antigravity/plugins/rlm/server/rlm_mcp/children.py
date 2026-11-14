import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

TASK_FILE_THRESHOLD = 100_000
PREVIEW_CHARS = 2_000
MAX_OUTPUT_CHARS = 64_000
STDERR_CAP = 8_192


@dataclass
class ChildResult:
    ok: bool
    output: str
    error: str | None
    usage: dict = field(default_factory=dict)
    turns: int = 0
    status: str = ""


def child_prompt(text: str, depth: int, max_depth: int, task_file: Path | None) -> str:
    head = (
        f"You are a recursive sub-RLM at depth {depth} (maximum {max_depth}). The task below was delegated "
        "by a parent RLM program, not a human. Your final message is returned to the parent as a plain "
        "string. Answer directly, keep it short and information-dense, and do not ask questions.\n\n"
    )
    if task_file is None:
        return head + text
    return head + (
        f"The full task text is {len(text)} chars, preloaded in the ipython kernel as the string "
        f"variable `task`. Read it with code instead of printing it. Preview:\n{text[:PREVIEW_CHARS]}"
    )


def build_argv(settings: Settings, prompt: str, model: str | None) -> list[str]:
    argv = [
        settings.agy,
        "-p",
        prompt,
        "--agent",
        "rlm",
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout",
        settings.child_timeout,
    ]
    if model:
        argv += ["--model", model]
    return argv


def add_usage(total: dict, usage: dict | None) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, dict):
            add_usage(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def parse_stream(lines: Iterable[str]) -> ChildResult:
    result = None
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            if msg.get("event") == "result" and isinstance(msg.get("result"), dict):
                result = msg["result"]
            elif msg.get("type") == "result":
                result = msg
    if result is None:
        return ChildResult(False, "", "child exited without a result record")
    status = str(result.get("status", ""))
    ok = status == "SUCCESS"
    output = str(result.get("response") or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n... [rlm output truncated at {MAX_OUTPUT_CHARS} chars]"
    error = None if ok else str(result.get("error") or f"child status {status or 'unknown'}")
    return ChildResult(
        ok,
        output,
        error,
        dict(result.get("usage") or {}),
        int(result.get("num_turns") or 0),
        status,
    )


def _log(file: Path | None, record) -> None:
    if file is None:
        return
    with open(file, "a") as f:
        f.write((record if isinstance(record, str) else json.dumps(record)).rstrip("\n") + "\n")


def run_child(
    settings: Settings,
    text: str,
    depth: int,
    model: str | None,
    log_dir: Path | None,
    parent_id: str | None,
    cwd: str | Path | None = None,
) -> ChildResult:
    child_id = uuid.uuid4().hex[:12]
    task_file = None
    if len(text) > TASK_FILE_THRESHOLD:
        tmp = Path(settings.data_dir) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        task_file = tmp / f"task-{child_id}.txt"
        task_file.write_text(text)
    env = {**os.environ, "PI_RLM_DEPTH": str(depth), "RLM_CHILD_ID": child_id}
    env.pop("RLM_TASK_FILE", None)
    env.pop("RLM_AGY_PID", None)
    if task_file:
        env["RLM_TASK_FILE"] = str(task_file)
    if parent_id:
        env["RLM_PARENT_ID"] = parent_id
    log_file = None
    if log_dir is not None and settings.child_log:
        log_dir.mkdir(parents=True, exist_ok=True)
        env["RLM_CHILD_LOG_DIR"] = str(log_dir)
        log_file = log_dir / f"{child_id}.jsonl"
    _log(
        log_file,
        {
            "type": "rlm_child_start",
            "id": child_id,
            "parent": parent_id,
            "depth": depth,
            "model": model,
            "ts": time.time(),
            "prompt": text[:PREVIEW_CHARS],
        },
    )
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            build_argv(
                settings,
                child_prompt(text, depth, settings.max_depth, task_file),
                model,
            ),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            _log(log_file, line)
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        result = parse_stream(lines)
        if code != 0 and result.ok:
            result.ok, result.error = False, f"child exited with code {code}"
        if not result.ok:
            result.error = f"{result.error}\n{stderr[-STDERR_CAP:]}".strip()
    except OSError as exc:
        result = ChildResult(False, "", f"could not start agy: {exc}")
    finally:
        if task_file is not None:
            task_file.unlink(missing_ok=True)
    _log(
        log_file,
        {
            "type": "rlm_child_end",
            "id": child_id,
            "ok": result.ok,
            "status": result.status,
            "usage": result.usage,
            "turns": result.turns,
            "ts": time.time(),
        },
    )
    return result
