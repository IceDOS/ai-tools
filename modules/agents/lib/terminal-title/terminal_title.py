from datetime import datetime
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import tool_state

IDLE_GLYPH = os.environ.get("ANTIGRAVITY_TITLE_IDLE_GLYPH") or "△"
WORKING_FRAMES = (
    os.environ.get("ANTIGRAVITY_TITLE_WORKING_GLYPHS", "").split()
    or ["▲", "▶", "▼", "◀"]
)
TICK = 0.25
TITLE_MAX = 80
TITLE_REFRESH = 2.0
FALLBACK = "Antigravity"
SUMMARIES = Path.home() / ".gemini/antigravity-cli/conversation_summaries.db"
STATE_DIR = tool_state.STATE_DIR
DEFAULT_PROCESS_NAMES = ["agy"]


def format_duration(seconds, always_seconds=False):
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        if always_seconds:
            return f"{h}h {m}m {s}s"
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    return f"{m}m {s}s" if always_seconds or s > 0 else f"{m}m"


def format_time(dt=None):
    if dt is None:
        dt = datetime.now()
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def compose_banner(elapsed, dt=None, glyph=IDLE_GLYPH):
    return f"{glyph} Worked for {format_duration(elapsed)} · done {format_time(dt)}"


def short_tool(name):
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def tool_timer(conversation, now=None):
    record = tool_state.load(conversation)
    if record is None:
        return ""
    elapsed = (time.time() if now is None else now) - record["start_ts"]
    return f"⏱ {short_tool(record['tool'])} {format_duration(elapsed, always_seconds=True)}"


def children_text(counter, state):
    agent_id = state.get("agent", state.get("agy"))
    depths = counter.by_depth(state.get("conversation"), agent_id)
    if not depths:
        return ""
    return f"{sum(depths.values())} rlm " + " ".join(f"d{d}:{n}" for d, n in sorted(depths.items()))


def proc_stat(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    state, ppid = stat[stat.rindex(")") + 2 :].split()[:2]
    if state == "Z":
        return None
    return stat[stat.index("(") + 1 : stat.rindex(")")], int(ppid)


def is_agent(pid, comm_names=None):
    stat = proc_stat(pid)
    names = comm_names or DEFAULT_PROCESS_NAMES
    return bool(stat) and stat[0] in names


def find_agent(comm_names=None):
    names = comm_names or DEFAULT_PROCESS_NAMES
    pid = os.getppid()
    while pid > 1 and (stat := proc_stat(pid)):
        if stat[0] in names:
            return pid
        pid = stat[1]
    return None


# Backward-compatibility aliases for antigravity
def is_agy(pid):
    return is_agent(pid, ["agy"])


def find_agy():
    return find_agent(["agy"])


def tty_path(agent_pid):
    for fd in (0, 1, 2):
        try:
            path = os.readlink(f"/proc/{agent_pid}/fd/{fd}")
        except OSError:
            continue
        if path.startswith("/dev/pts/") or (path.startswith("/dev/tty") and path[8:].isdigit()):
            return path
    return None


def emit(fd, title):
    try:
        os.write(fd, f"\033]0;{title}\007".encode())
    except OSError:
        pass


def emit_to_tty(tty, title=""):
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
        emit(fd, title)
        os.close(fd)
    except OSError:
        pass


def conversation_title(conversation):
    if not conversation:
        return ""
    try:
        if SUMMARIES.exists():
            with sqlite3.connect(f"file:{SUMMARIES}?mode=ro", uri=True, timeout=0.5) as db:
                row = db.execute(
                    "select title, preview from conversation_summaries where conversation_id = ?",
                    (conversation,),
                ).fetchone()
                return next((t for t in row or () if t and t.strip()), "")
    except sqlite3.Error:
        pass
    return ""


def title_text(state, fallback=None):
    name = state.get("name")
    if not name and state.get("conversation"):
        name = conversation_title(state.get("conversation"))
    text = " ".join((name or "").split())
    if not text:
        text = fallback or state.get("fallback") or FALLBACK
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1] + "…"


def compose_title(glyph, text, timer=""):
    suffix = f" · {timer}" if timer else ""
    room = TITLE_MAX - len(glyph) - 1 - len(suffix)
    if room <= 0:
        return f"{glyph} {text}"
    if len(text) > room:
        text = text[: room - 1] + "…"
    return f"{glyph} {text}{suffix}"


def state_file(tty, suffix):
    return STATE_DIR / (tty.strip("/").replace("/", "-") + suffix)


def _owner(state):
    return state.get("agent", state.get("agy"))


def load(tty, owner=None):
    try:
        data = json.loads(state_file(tty, ".json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # pts numbers get reused: state left by a previous owner of this tty must not leak in.
    if owner is not None and _owner(data) not in (None, owner):
        return {}
    return data


def save(tty, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_file(tty, ".json")
    merged = load(tty, owner=_owner(state))
    merged.update(state)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(merged))
    os.replace(tmp, path)


def ticker_lock(tty):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_file(tty, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def start_ticker(tty):
    if (lock := ticker_lock(tty)) is not None:
        os.close(lock)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "tick", tty],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )


def tick(tty):
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return
    lock = ticker_lock(tty)
    if lock is None:
        os.close(fd)
        return
    frame, text, shown, seen, refresh_at = 0, "", None, None, 0.0
    counter = tool_state.ChildrenCounter(tool_state.rlm_data_dir())
    while True:
        state = load(tty)
        pid = state.get("agent", state.get("agy", 0))
        comm_names = state.get("comm_names") or DEFAULT_PROCESS_NAMES
        if isinstance(comm_names, str):
            comm_names = comm_names.split() or DEFAULT_PROCESS_NAMES
        if not is_agent(pid, comm_names=comm_names):
            emit(fd, "")
            break
        now, working = time.monotonic(), state.get("working")
        if state != seen or now >= refresh_at:
            if state != seen:
                shown = None  # a hook may have cleared the title (SessionEnd on /resume), so send it again
            text, seen, refresh_at = title_text(state, fallback=state.get("fallback")), state, now + TITLE_REFRESH
        timer = tool_timer(state.get("conversation"))
        children = children_text(counter, state)
        if children:
            timer = f"{timer} · {children}" if timer else children
        working = working or bool(timer)
        frames = state.get("working_frames") or WORKING_FRAMES
        if isinstance(frames, str):
            frames = frames.split() or WORKING_FRAMES
        idle = state.get("idle_glyph") or IDLE_GLYPH
        glyph = frames[frame % len(frames)] if working else idle
        suffix = timer if working else ""
        if (title := compose_title(glyph, text, suffix)) != shown:
            shown = title
            emit(fd, title)
        if working:
            frame += 1
        time.sleep(TICK)
    os.close(lock)
    os.close(fd)


def update(payload, start_ticker=True):
    agy = find_agy()
    if agy is None or not (tty := tty_path(agy)):
        return None
    cur = load(tty, owner=agy)
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
        os.close(lock)
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
