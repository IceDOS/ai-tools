import json
import os
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

STALE_SECONDS = 600


@runtime_checkable
class SessionSource(Protocol):
    session_key: str

    def resolve_workspace(self) -> str:
        ...

    def read_session(self, data_dir: Path, cwd: str | None = None) -> dict:
        ...

    def update_session(self, data_dir: Path, fields: dict) -> dict:
        ...

    def control_socket_path(self, data_dir: Path) -> Path:
        ...

    def conversation_id(self, data_dir: Path, cwd: str | None = None) -> str | None:
        ...


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


def session_path(data_dir: Path, key: int | str) -> Path:
    return Path(data_dir) / "sessions" / f"{key}.json"


def control_socket_path(data_dir: Path, key: int | str) -> Path:
    p = Path(data_dir) / "run" / f"{key}.sock"
    if len(str(p).encode("utf-8")) >= 104:
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        rundir = Path(f"/run/user/{uid}/icedos-rlm")
        return (rundir if rundir.parent.exists() else Path(f"/tmp/icedos-rlm-{uid}")) / f"{key}.sock"
    return p


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def update_session(data_dir: Path, key: int | str, fields: dict) -> dict:
    path = session_path(data_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**(_load(path) or {}), **fields}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged))
    os.replace(tmp, path)
    return merged


def read_session(data_dir: Path, key: int | str, cwd: str | None = None) -> dict | None:
    data = _load(session_path(data_dir, key))
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


class AgySessionSource:
    def __init__(self, pid: int | None = None):
        self.pid = find_agy_pid() if pid is None else pid

    @property
    def session_key(self) -> str:
        return str(self.pid)

    def resolve_workspace(self) -> str:
        return resolve_workspace(self.pid)

    def read_session(self, data_dir: Path, cwd: str | None = None) -> dict:
        return read_session(data_dir, self.pid, cwd=cwd) or {}

    def update_session(self, data_dir: Path, fields: dict) -> dict:
        return update_session(data_dir, self.pid, fields)

    def control_socket_path(self, data_dir: Path) -> Path:
        return control_socket_path(data_dir, self.pid)

    def conversation_id(self, data_dir: Path, cwd: str | None = None) -> str | None:
        return self.read_session(data_dir, cwd=cwd).get("conversationId")


class OpencodeSessionSource:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or os.environ.get("OPENCODE_SESSION_ID", "default")

    @property
    def session_key(self) -> str:
        return self.session_id

    def resolve_workspace(self) -> str:
        return os.getcwd()

    def read_session(self, data_dir: Path, cwd: str | None = None) -> dict:
        sess = _load(session_path(data_dir, self.session_key))
        if sess is not None:
            return sess
        return {
            "conversationId": self.session_id,
            "workspacePaths": [self.resolve_workspace()],
        }

    def update_session(self, data_dir: Path, fields: dict) -> dict:
        return update_session(data_dir, self.session_key, fields)

    def control_socket_path(self, data_dir: Path) -> Path:
        return control_socket_path(data_dir, self.session_key)

    def conversation_id(self, data_dir: Path, cwd: str | None = None) -> str | None:
        return self.session_id


def get_session_source(
    pid: int | None = None, key: str | None = None, runner: str | None = None
) -> SessionSource:
    r = (runner or os.environ.get("RLM_RUNNER") or "agy").lower()
    if r == "opencode" or (key is not None and not str(key).isdigit()):
        return OpencodeSessionSource(session_id=key)
    return AgySessionSource(pid)
