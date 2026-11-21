import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    agy: str
    kernel: str
    python: str
    data_dir: Path
    max_depth: int
    depth: int
    child_timeout: str
    child_log: bool
    snapshots: bool
    self_cmd: list[str]
    runner: str = "agy"
    opencode: str = "opencode"
    max_parallel: int = 4

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        home = env.get("HOME") or os.path.expanduser("~")
        data = env.get("PLUGIN_DATA") or os.path.join(
            env.get("XDG_DATA_HOME") or os.path.join(home, ".local/share"), "icedos-rlm"
        )
        self_cmd = [env["RLM_SELF"]] if env.get("RLM_SELF") else [sys.argv[0]]
        return cls(
            agy=env.get("RLM_AGY", "agy"),
            kernel=env.get("RLM_KERNEL", ""),
            python=env.get("RLM_PYTHON", sys.executable),
            data_dir=Path(data),
            max_depth=int(env.get("RLM_MAX_DEPTH", "2") or "2"),
            depth=int(env.get("PI_RLM_DEPTH", "0") or "0"),
            child_timeout=env.get("RLM_CHILD_TIMEOUT", "15m"),
            child_log=env.get("RLM_CHILD_LOG", "1") == "1",
            snapshots=env.get("RLM_SNAPSHOTS", "1") == "1",
            self_cmd=self_cmd,
            runner=env.get("RLM_RUNNER", "agy"),
            opencode=env.get("RLM_OPENCODE", "opencode"),
            max_parallel=max(1, int(env.get("RLM_MAX_PARALLEL", "4") or "4")),
        )
