"""Antigravity CLI terminal title, "<glyph> <title>" over OSC 0.

agy's own `title` hook only reruns on state changes, so it cannot animate. The status line reruns on
every state change instead: it saves the state and starts a detached ticker, which draws the glyph,
animates it while the agent works, follows the conversation title, and clears it once agy exits.
"""
from datetime import datetime
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

IDLE_GLYPH = os.environ.get("ANTIGRAVITY_TITLE_IDLE_GLYPH") or "▲"
WORKING_FRAMES = os.environ.get("ANTIGRAVITY_TITLE_WORKING_GLYPHS", "").split() or ["▖", "▘", "▝", "▗"]
TICK = 0.25
TITLE_MAX = 80
# agy writes a conversation's generated title a few seconds into its first turn.
TITLE_REFRESH = 2.0
FALLBACK = "Antigravity"

SUMMARIES = Path.home() / ".gemini/antigravity-cli/conversation_summaries.db"
STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/antigravity-title-{os.getuid()}") / "antigravity-title"


def format_duration(seconds):
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    return f"{m}m {s}s" if s > 0 else f"{m}m"


def format_time(dt=None):
    if dt is None:
        dt = datetime.now()
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def compose_banner(elapsed, dt=None, glyph=IDLE_GLYPH):
    return f"{glyph} Worked for {format_duration(elapsed)} · done {format_time(dt)}"


def proc_stat(pid):
    """(comm, ppid) of pid, or None once it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.index("(") + 1 : stat.rindex(")")], int(stat[stat.rindex(")") + 2 :].split()[1])


def is_agy(pid):
    stat = proc_stat(pid)
    return bool(stat) and stat[0] == "agy"


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


def emit(fd, title):
    """Set the title in one write, so agy's own frames never split the sequence."""
    try:
        os.write(fd, f"\033]0;{title}\007".encode())
    except OSError:
        pass


def conversation_title(conversation):
    """The title agy generated for the conversation, else its first prompt."""
    if not conversation:
        return ""
    try:
        with sqlite3.connect(f"file:{SUMMARIES}?mode=ro", uri=True, timeout=0.5) as db:
            row = db.execute(
                "select title, preview from conversation_summaries where conversation_id = ?",
                (conversation,),
            ).fetchone()
    except sqlite3.Error:
        return ""
    return next((t for t in row or () if t and t.strip()), "")


def title_text(state):
    """A name the user gave the conversation, else agy's title, else the agent name."""
    text = " ".join((state.get("name") or conversation_title(state.get("conversation")) or "").split())
    if not text:
        return FALLBACK
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1] + "…"


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
    merged = load(tty)
    merged.update(state)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(merged))
    os.replace(tmp, path)


def ticker_lock(tty):
    """This terminal's ticker lock, or None while a ticker holds it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_file(tty, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


# ---------- the two entry points ----------
def tick(tty):
    """Draw the title until agy exits, then hand the title back to the shell."""
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return
    lock = ticker_lock(tty)
    if lock is None:  # another ticker owns this terminal
        os.close(fd)
        return
    frame, text, shown, seen, refresh_at = 0, "", None, None, 0.0
    while True:
        state = load(tty)
        if not is_agy(state.get("agy", 0)):
            emit(fd, "")  # agy is gone, so drop our title
            break
        now, working = time.monotonic(), state.get("working")
        if state != seen or now >= refresh_at:
            # agy can generate the conversation title mid-turn
            text, seen, refresh_at = title_text(state), state, now + TITLE_REFRESH
        glyph = WORKING_FRAMES[frame % len(WORKING_FRAMES)] if working else IDLE_GLYPH
        if (title := f"{glyph} {text}") != shown:
            shown = title
            emit(fd, title)
        if working:  # only the working glyph animates
            frame += 1
        time.sleep(TICK)
    os.close(lock)
    os.close(fd)


def update(payload, start_ticker=True):
    """Save state and turn timer, start ticker if requested, return current banner."""
    agy = find_agy()
    if agy is None or not (tty := tty_path(agy)):
        return None
    cur = load(tty)
    working = payload.get("agent_state") == "working"
    was_working = bool(cur.get("working"))
    turn_start = cur.get("turn_start")
    banner = cur.get("banner")

    if working and not was_working:
        turn_start = time.time()
        banner = None
    elif not working and was_working and turn_start is not None:
        elapsed = time.time() - turn_start
        banner = compose_banner(elapsed)
        turn_start = None

    save(tty, {
        "agy": agy,
        "conversation": payload.get("conversation_id") or None,
        "name": payload.get("conversation_title") or None,
        "working": working,
        "turn_start": turn_start,
        "banner": banner,
    })
    if start_ticker and (lock := ticker_lock(tty)) is not None:
        os.close(lock)  # hand the lock to the ticker
        # no inherited fds: agy reads the status line until its stdout closes
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "tick", tty],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    return banner


if __name__ == "__main__":
    if sys.argv[1:2] == ["tick"] and len(sys.argv) == 3:
        tick(sys.argv[2])
    else:
        sys.exit("usage: terminal_title.py tick TTY")
