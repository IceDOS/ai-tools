"""The tool call agy is running, per conversation, for the terminal title.

agy redraws its status bar only on state changes, not during a tool call, so only the
title's ticker can count: a PreToolUse hook records the call here and the ticker draws it.
The rlm() children still running are counted from the child logs the plugin writes.
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


# The rlm plugin's data dir (icedos sets this to the dir it configures): unset means the
# plugin is off, so no surface shows children. `children/<conversation>/<id>.jsonl` per child.
RLM_DATA_ENV = "ANTIGRAVITY_RLM_DATA_DIR"
# A scan lists the log dir at most this often, and reads only this much of a log's end.
CHILD_SCAN = 1.0
CHILD_TAIL = 1024


def rlm_data_dir():
    """The rlm plugin's data dir, or None while the plugin is off."""
    value = os.environ.get(RLM_DATA_ENV)
    return Path(value) if value else None


def children_dir(data_dir, conversation):
    """The dir the plugin logs one conversation's children in, or None without one."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", conversation or "")
    return Path(data_dir) / "children" / name if name and data_dir else None


def session_conversation(data_dir, agy):
    """The conversation the rlm server logs under, from its <data_dir>/sessions/<agy>.json."""
    if not data_dir or not agy:
        return None
    try:
        record = json.loads((Path(data_dir) / "sessions" / f"{agy}.json").read_text())
    except (OSError, ValueError):
        return None
    return record.get("conversationId") if isinstance(record, dict) else None


def pid_alive(pid):
    """Whether that process still runs; a zombie keeps its /proc entry until it is reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def child_start(record_path):
    """The record a child wrote when it started, or None when its log is unreadable."""
    try:
        with open(record_path, "rb") as log:
            record = json.loads(log.readline())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def child_running(record_path, pid=None):
    """A child runs until its log ends it or the rlm server that started it is gone.

    `pid` is the server's pid from the start record: a killed server or an interrupted cell
    never writes the end record. Logs from before that field keep the record-only rule.
    """
    try:
        size = Path(record_path).stat().st_size
        with open(record_path, "rb") as log:
            log.seek(max(0, size - CHILD_TAIL))
            last = log.read().rstrip(b"\n").rsplit(b"\n", 1)[-1].lstrip()
    except OSError:
        return False
    try:
        ended = json.loads(last).get("type") == "rlm_child_end"
    except (ValueError, AttributeError):
        ended = False  # a half-written line: the child is still running
    return not ended and (pid_alive(pid) if pid else True)


class ChildrenCounter:
    """The rlm() children of a conversation that still run, from their JSONL logs.

    A child writes one log and never reopens it, so an ended log is remembered and skipped;
    the rest are re-read a tail at a time, and no scan runs more often than `interval`.
    A log whose start record names a dead server counts as ended: nothing will close it.
    """

    def __init__(self, data_dir=None, interval=CHILD_SCAN, clock=time.monotonic):
        self.data_dir = data_dir
        self.interval = interval
        self.clock = clock
        self.ended = set()
        self.starts = {}
        self.scans = {}

    def count(self, conversation=None, agy=None):
        """How many children of that conversation run right now (cached between scans)."""
        return sum(self.by_depth(conversation, agy).values())

    def by_depth(self, conversation=None, agy=None):
        """{depth: running children} for that conversation (cached between scans)."""
        if not self.data_dir:
            return {}
        now = self.clock()
        if (cached := self.scans.get((conversation, agy))) and now - cached[0] < self.interval:
            return cached[1]
        running = self._scan(conversation, agy)
        self.scans[(conversation, agy)] = (now, running)
        return running

    def _scan(self, conversation, agy):
        ids = (conversation, session_conversation(self.data_dir, agy))
        log_dir = next((d for i in ids if (d := children_dir(self.data_dir, i)) and d.is_dir()), None)
        if log_dir is None:
            return {}
        try:
            logs = [p for p in log_dir.iterdir() if p.name.endswith(".jsonl")]
        except OSError:  # gc may prune the dir under us
            return {}
        running = {}
        for path in logs:
            if path in self.ended:
                continue
            if path not in self.starts:  # the start record is read once per log
                record = child_start(path) or {}
                pid, depth = record.get("pid"), record.get("depth")
                self.starts[path] = (pid if isinstance(pid, int) else None, depth if isinstance(depth, int) else 1)
            pid, depth = self.starts[path]
            if child_running(path, pid):
                running[depth] = running.get(depth, 0) + 1
            else:
                self.ended.add(path)
        return running
