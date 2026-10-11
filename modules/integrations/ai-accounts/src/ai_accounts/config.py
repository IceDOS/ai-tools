"""Paths and the per-user config Nix writes to ~/.config/ai-accounts/config.json."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

AGENTS = ("claude", "prime-agent", "opencode", "antigravity", "ollama")


def _xdg(name: str, default: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else Path.home() / default


def config_path() -> Path:
    override = os.environ.get("AI_ACCOUNTS_CONFIG")
    return Path(override) if override else _xdg("XDG_CONFIG_HOME", ".config") / "ai-accounts" / "config.json"


def state_dir() -> Path:
    override = os.environ.get("AI_ACCOUNTS_STATE")
    return Path(override) if override else _xdg("XDG_STATE_HOME", ".local/state") / "ai-accounts"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "ai-accounts"


def alimit_db() -> Path:
    # Resolved before an adapter overrides XDG_DATA_HOME, so every account writes one database.
    pinned = os.environ.get("ALIMIT_DB")
    return Path(pinned) if pinned else _xdg("XDG_DATA_HOME", ".local/share") / "alimit" / "usage.db"


@dataclass(frozen=True)
class Account:
    agent: str
    name: str
    mode: str = "shared"
    label: str = ""
    settings: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def dir(self) -> Path:
        return state_dir() / self.agent / self.name

    @property
    def is_default(self) -> bool:
        return self.name == "default"


@dataclass
class Config:
    select: str = "manual"
    threshold: int = 90
    bus_talk: list = field(default_factory=list)
    alimit: str = ""
    real_dirs: dict = field(default_factory=dict)
    bins: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    managed: dict = field(default_factory=dict)


def load(path: Path | None = None) -> Config:
    try:
        raw = json.loads(Path(path or config_path()).read_text())
    except FileNotFoundError:
        raw = {}
    cfg = Config(
        select=raw.get("select", "manual"),
        threshold=int(raw.get("threshold", 90)),
        bus_talk=list(raw.get("busTalk", [])),
        alimit=raw.get("alimit", ""),
    )
    for agent, spec in (raw.get("agents") or {}).items():
        cfg.managed[agent] = spec.get("settings") or {}
        cfg.real_dirs[agent] = spec.get("realDir") or ""
        cfg.bins[agent] = spec.get("bin") or ""
        declared = spec.get("accounts") or {}
        accounts = {"default": Account(agent, "default")} if declared else {}
        for name, a in declared.items():
            accounts[name] = Account(agent, name, a.get("mode", "shared"), a.get("label", ""), a.get("settings") or {})
        cfg.accounts[agent] = accounts
    return cfg
