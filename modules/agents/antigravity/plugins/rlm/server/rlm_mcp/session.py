import json
import os
import time
from pathlib import Path

STALE_SECONDS = 600


def _comm_and_parent(pid: int) -> tuple[str, int] | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            head, tail = f.read().rsplit(")", 1)
        return head.split("(", 1)[1], int(tail.split()[1])
    except (OSError, ValueError, IndexError):
        return None


def find_agy_pid(start: int | None = None) -> int:
    if os.environ.get("RLM_AGY_PID"):
        return int(os.environ["RLM_AGY_PID"])
    pid = os.getppid() if start is None else start
    for _ in range(16):
        info = _comm_and_parent(pid)
        if info is None:
            break
        comm, parent = info
        if comm.startswith("agy") or "antigravity" in comm:
            return pid
        if parent <= 1:
            break
        pid = parent
    return os.getppid()


def resolve_workspace(agy_pid: int) -> str:
    try:
        return os.readlink(f"/proc/{agy_pid}/cwd")
    except OSError:
        return os.getcwd()


def session_path(data_dir: Path, pid: int) -> Path:
    return Path(data_dir) / "sessions" / f"{pid}.json"


def control_socket_path(data_dir: Path, pid: int) -> Path:
    p = Path(data_dir) / "run" / f"{pid}.sock"
    if len(str(p).encode("utf-8")) >= 104:
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        rundir = Path(f"/run/user/{uid}/icedos-rlm")
        return (rundir if rundir.parent.exists() else Path(f"/tmp/icedos-rlm-{uid}")) / f"{pid}.sock"
    return p


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def update_session(data_dir: Path, pid: int, fields: dict) -> dict:
    path = session_path(data_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**(_load(path) or {}), **fields}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged))
    os.replace(tmp, path)
    return merged


def read_session(data_dir: Path, pid: int, cwd: str | None = None) -> dict | None:
    data = _load(session_path(data_dir, pid))
    if data is not None or cwd is None:
        return data
    best, best_mtime = None, 0.0
    for path in (Path(data_dir) / "sessions").glob("*.json"):
        mtime = path.stat().st_mtime
        if time.time() - mtime > STALE_SECONDS or mtime <= best_mtime:
            continue
        candidate = _load(path)
        if candidate and cwd in (candidate.get("workspacePaths") or []):
            best, best_mtime = candidate, mtime
    return best
