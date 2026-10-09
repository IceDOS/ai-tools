"""Antigravity CLI terminal title, "<status glyph> <headline>" like Claude Code and prime-agent.

agy never sets one. It reruns the status line command on every agent state change, so that call
drives the title, and a detached ticker animates the working glyph between calls.
"""
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

IDLE_GLYPH = os.environ.get("ANTIGRAVITY_TITLE_IDLE_GLYPH") or "◉"
WORKING_FRAMES = os.environ.get("ANTIGRAVITY_TITLE_WORKING_GLYPHS", "").split() or ["◐", "◓", "◑", "◒"]
TICK = 0.25
# agy writes a conversation's title a few seconds into its first turn.
HEADLINE_REFRESH = 2.0
HEADLINE_MAX = 80

SUMMARIES = Path.home() / ".gemini/antigravity-cli/conversation_summaries.db"
STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/antigravity-title-{os.getuid()}") / "antigravity-title"


def proc_stat(pid):
    """(comm, ppid) of pid, or None once it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    comm = stat[stat.index("(") + 1 : stat.rindex(")")]
    return comm, int(stat[stat.rindex(")") + 2 :].split()[1])


def find_agy():
    """The agy process running this status line (it may sit under an `sh -c`)."""
    pid = os.getppid()
    while pid > 1 and (stat := proc_stat(pid)):
        if stat[0] == "agy":
            return pid
        pid = stat[1]
    return None


def tty_path(agy_pid):
    """agy's terminal device; the detached ticker has no /dev/tty to open."""
    for fd in (0, 1, 2):
        try:
            path = os.readlink(f"/proc/{agy_pid}/fd/{fd}")
        except OSError:
            continue
        if path.startswith("/dev/pts/") or (path.startswith("/dev/tty") and path[8:].isdigit()):
            return path
    return None


def emit(tty, title):
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return
    try:
        # one write, so agy's own frames never split the sequence
        os.write(fd, f"\033]0;{title}\007".encode())
    finally:
        os.close(fd)


def headline(state):
    """Conversation title, else its first prompt, else "Antigravity - <dir>"."""
    text = ""
    if conversation := state.get("conversation"):
        try:
            with sqlite3.connect(f"file:{SUMMARIES}?mode=ro", uri=True, timeout=0.5) as db:
                row = db.execute(
                    "select title, preview from conversation_summaries where conversation_id = ?",
                    (conversation,),
                ).fetchone()
            text = next((t for t in row or () if t and t.strip()), "")
        except sqlite3.Error:
            pass
    text = " ".join(text.split())
    if not text:
        return f"Antigravity - {os.path.basename(state.get('cwd') or '') or '/'}"
    return text if len(text) <= HEADLINE_MAX else text[: HEADLINE_MAX - 1] + "…"


# ---------- per-terminal state ----------
def state_file(tty, suffix):
    return STATE_DIR / (tty.strip("/").replace("/", "-") + suffix)


def load(tty):
    try:
        data = json.loads(state_file(tty, ".json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(tty, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_file(tty, ".json")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def ticker_lock(tty, blocking):
    """The ticker's per-terminal flock, or None while another process holds it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_file(tty, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if not blocking or time.monotonic() > deadline:
                os.close(fd)
                return None
            time.sleep(0.02)


def tick(tty):
    if (lock := ticker_lock(tty, blocking=False)) is None:
        return
    frame, text, refresh_at = 0, None, 0.0
    while True:
        state = load(tty)
        if not state.get("working") or proc_stat(state.get("agy", 0)) is None:
            break
        if time.monotonic() >= refresh_at:
            text, refresh_at = headline(state), time.monotonic() + HEADLINE_REFRESH
        emit(tty, f"{WORKING_FRAMES[frame % len(WORKING_FRAMES)]} {text}")
        frame += 1
        time.sleep(TICK)
    os.close(lock)


def update(payload):
    """Apply one status line payload: start the ticker, or show the idle title."""
    agy = find_agy()
    if agy is None or not (tty := tty_path(agy)):
        return
    state = load(tty)
    working = payload.get("agent_state") == "working"
    workspace = payload.get("workspace") or {}
    new = dict(
        state,
        agy=agy,
        conversation=payload.get("conversation_id") or None,
        cwd=workspace.get("current_dir") or payload.get("cwd"),
        working=working,
    )
    if working:
        if new != state:
            save(tty, new)
        if (lock := ticker_lock(tty, blocking=False)) is not None:
            os.close(lock)
            # no inherited fds: agy reads the status line until its stdout closes
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "tick", tty],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True,
            )
        return
    title = f"{IDLE_GLYPH} {headline(new)}"
    new["shown"] = title
    if new != state:
        save(tty, new)
    if state.get("working"):
        # wait out the ticker so its last working frame cannot land after the idle title
        if (lock := ticker_lock(tty, blocking=True)) is not None:
            os.close(lock)
    elif title == state.get("shown") and state.get("agy") == agy:
        return
    emit(tty, title)


if __name__ == "__main__":
    if sys.argv[1:2] == ["tick"] and len(sys.argv) == 3:
        tick(sys.argv[2])
    else:
        sys.exit("usage: terminal_title.py tick TTY")
