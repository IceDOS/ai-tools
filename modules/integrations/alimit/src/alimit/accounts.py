"""ai-accounts' declared accounts, as alimit polls and shows them. Read-only."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import config


def _xdg(name: str, default: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else config.HOME / default


def config_path() -> Path:
    override = os.environ.get("AI_ACCOUNTS_CONFIG")
    return Path(override) if override else _xdg("XDG_CONFIG_HOME", ".config") / "ai-accounts" / "config.json"


def state_dir() -> Path:
    override = os.environ.get("AI_ACCOUNTS_STATE")
    return Path(override) if override else _xdg("XDG_STATE_HOME", ".local/state") / "ai-accounts"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


CREDENTIALS = {
    "claude": ".credentials.json",
    "prime-agent": "auth.json",
    "opencode": "opencode/auth.json",
    "ollama": "key",
}


@dataclass(frozen=True)
class Account:
    agent: str
    name: str

    @property
    def tag(self) -> str:
        return config.account_tag(self.agent, self.name)

    @property
    def credentials(self) -> Path:
        return state_dir() / self.agent / self.name / CREDENTIALS[self.agent]


def declared() -> list:
    agents = read_json(config_path()).get("agents") or {}
    return [
        Account(agent, name)
        for agent, spec in agents.items()
        if agent in CREDENTIALS
        for name in (spec.get("accounts") or {})
    ]


def env_tags():
    raw = os.environ.get("AI_ACCOUNT_TAGS")
    if not raw:
        return None
    return dict(part.split("=", 1) for part in raw.split(",") if "=" in part)
from . import store
from .rates import to_ms


@dataclass(frozen=True)
class Headroom:
    account: str
    max_util: float
    reset_ts: int | None


def headroom(con, agent: str, names: list, now_ms: int) -> list:
    providers = config.AGENT_PROVIDERS[agent]
    present = [w for w in store.windows_present(con) if config.provider_of(w) in providers]
    out = []
    for name in names:
        tag = config.account_tag(agent, name)
        best = Headroom(name, 0.0, None)
        for w in present:
            if config.split_account(w)[1] != tag:
                continue
            rows = store.samples_for(con, w, since_ts=now_ms - config.HISTORY_MS)
            if not rows:
                continue
            _, util, resets_at, _ = rows[-1]
            reset = to_ms(resets_at)
            if reset is not None and reset <= now_ms:
                continue
            if util > best.max_util:
                best = Headroom(name, util, reset)
        out.append(best)
    return out


def rank(rows: list, threshold: float, eligible: list):
    order = {n: i for i, n in enumerate(eligible)}
    rows = [r for r in rows if r.account in order]
    under = [r for r in rows if r.max_util < threshold]
    if under:
        return min(under, key=lambda r: (r.max_util, order[r.account])).account
    if rows:
        return min(rows, key=lambda r: (r.reset_ts is None, r.reset_ts or 0, order[r.account])).account
    return None


def agent_account(window: str):
    base, tag = config.split_account(window)
    provider = config.provider_of(base)
    agent, dot, name = tag.partition(".")
    if dot and agent in config.MULTI_PROVIDER_AGENTS:
        return agent, name
    if provider in ("claude", "antigravity", "ollama"):
        return provider, tag
    return None


def marks(con, now_ms: int) -> dict:
    """(agent, account) -> "★" for the sticky account, "→" for auto-pick's next choice."""
    cfg = read_json(config_path())
    sticky = read_json(state_dir() / "sticky.json")
    status = read_json(state_dir() / "status.json")
    out = {}
    for agent, spec in (cfg.get("agents") or {}).items():
        names = ["default", *(spec.get("accounts") or {})]
        if len(names) < 2 or agent not in config.AGENT_PROVIDERS:
            continue
        chosen = sticky.get(agent)
        if chosen and chosen != "auto":
            out[(agent, chosen)] = "★"
            continue
        if cfg.get("select") == "auto" or chosen == "auto":
            logged = status.get(agent) or {}
            eligible = [n for n in names if logged.get(n, n == "default")]
            pick = rank(headroom(con, agent, eligible, now_ms), cfg.get("threshold", 90), eligible)
            if pick:
                out[(agent, pick)] = "→"
    return out