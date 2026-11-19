"""The tool call agy is running, per conversation, for the terminal title.

agy redraws its status bar only on state changes, not during a tool call, so only the
title's ticker can count: a PreToolUse hook records the call here and the ticker draws it.
"""
import json
import os
import re
import time
from pathlib import Path

# The runtime dir the title ticker already uses; the hook and the ticker are
# separate processes, so this path is their only shared state.
STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/antigravity-title-{os.getuid()}") / "antigravity-title"
# A call whose PostToolUse never ran (agy killed mid-turn) must not count forever; 15 minutes is
# above the 10 minute longest normal tool timeout.
MAX_AGE = 900.0


def path(conversation):
    """The state file for a conversation, or None without one."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", conversation or "")
    return STATE_DIR / "tools" / f"{name}.json" if name else None


def save(conversation, tool, call_id=None, step=None, started=None):
    """Record the call that just started; a tool call with no name records nothing."""
    record_path = path(conversation)
    if record_path is None or not tool:
        return
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"tool": tool, "start_ts": time.time() if started is None else started}
    if call_id:
        record["id"] = call_id
    if step is not None:
        record["step"] = step
    # Atomic: the ticker reads this file every quarter second.
    tmp = record_path.with_name(f"{record_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, record_path)


def load(conversation):
    """The running call, or None once it ended, went stale, or was never written."""
    record_path = path(conversation)
    if record_path is None:
        return None
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("tool"):
        return None
    started = record.get("start_ts")
    if not isinstance(started, (int, float)) or time.time() - started > MAX_AGE:
        return None
    return record


def clear(conversation, call_id=None):
    """Drop the record; with a call id, only while it still describes that call."""
    record_path = path(conversation)
    if record_path is None:
        return
    if call_id:
        record = load(conversation)
        if record is not None and record.get("id") not in (None, call_id):
            return
    try:
        record_path.unlink()
    except OSError:
        pass
