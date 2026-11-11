"""agent-relay ID and peer count for the agent status line."""

import json
import os
from pathlib import Path


def _root() -> Path:
    if p := os.environ.get("AGENT_RELAY_ROOT"):
        return Path(p)
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "agent-relay"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _ancestors(pid: int) -> list[int]:
    out = []
    while pid > 1 and len(out) < 32:
        out.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            break
    return out


def segment(pids=None, root=None, alive=None):
    if os.environ.get("ALIMIT_RELAY_SEGMENT") == "0":
        return None
    root = root or _root()
    alive = alive or _alive
    recs = []
    for f in (root / "sessions").glob("*.json"):
        try:
            recs.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    recs = [r for r in recs if alive(r.get("pid", 0))]
    by_pid = {r["pid"]: r for r in recs}
    me = next((by_pid[p] for p in (pids or _ancestors(os.getppid())) if p in by_pid), None)
    if not me:
        return None
    peers = sum(1 for r in recs if me.get("group") and r.get("group") == me["group"] and r["id"] != me["id"])
    return f"relay {me['id']}" + (f" · {peers} peer{'s' if peers != 1 else ''}" if peers else "")
