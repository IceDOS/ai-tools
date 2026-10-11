"""Per-agent environment, private entries, login and logged-in checks."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import config, dbusproxy, mirror, settings
from .config import Account, Config

CLAUDE_PRIVATE = frozenset({".credentials.json", ".claude.json"})
PRIME_PRIVATE = frozenset({"auth.json", "daemon-workers", "supervisor-owners", "session-leases"})
# Task 1: the socket is $TMPDIR/prime-agent-<uid>/daemon.sock, not under the agent dir.
PRIME_DAEMON_SOCKET_FOLLOWS_DIR = False
# agy keeps per account: the token file plus its project and onboarding cache (probe 2026-10-10).
AGY_PRIVATE: frozenset = frozenset({
    "antigravity-cli/antigravity-oauth-token",
    "antigravity-cli/cache/default_project_id.txt",
    "antigravity-cli/cache/onboarding.json",
    "antigravity-cli/cli.log",
    "antigravity-cli/log",
})
AGY_CREDS: tuple = ("antigravity-cli/antigravity-oauth-token",)

CLAUDE_SEED = frozenset({"settings.json"})
PRIME_SEED = frozenset({"settings.json"})
# agy rewrites these with rename-over-save, so a symlink would not survive (2026-10-11, Step 1 of Task 16).
AGY_SEED = frozenset({"antigravity-cli/settings.json", "antigravity-cli/cache",
                        "antigravity-cli/jetbox_summaries_proto.pb"})
SETTINGS_FILE = {
    "claude": "settings.json",
    "prime-agent": "settings.json",
    "antigravity": "home/.gemini/antigravity-cli/settings.json",
    "opencode": "opencode-settings.json",
}
LEDGER = ".ai-account-settings.json"

CREDS = {
    "claude": (".credentials.json",),
    "prime-agent": ("auth.json",),
    "opencode": ("opencode/auth.json",),
    "ollama": ("key",),
}


class AdapterError(Exception):
    pass


class NotLoggedIn(Exception):
    pass


@dataclass
class Launch:
    env: dict
    args: list
    keep_fds: tuple = ()


def _mirrored(acct: Account, real: str, private, seed=frozenset()) -> None:
    if acct.mode == "isolated":
        acct.dir.mkdir(parents=True, exist_ok=True)
        return
    with mirror.locked(f"{acct.agent}-{acct.name}"):
        mirror.sync(Path(real), acct.dir, private, seed=seed)


def _claude(acct, cfg):
    _mirrored(acct, cfg.real_dirs["claude"], CLAUDE_PRIVATE, CLAUDE_SEED)
    return Launch({"CLAUDE_CONFIG_DIR": str(acct.dir)}, [])


def _prime_agent(acct, cfg):
    _mirrored(acct, cfg.real_dirs["prime-agent"], PRIME_PRIVATE, PRIME_SEED)
    args = []
    if not PRIME_DAEMON_SOCKET_FOLLOWS_DIR:
        args = ["--daemon-socket", str(config.runtime_dir() / f"prime-agent-{acct.name}.sock")]
    return Launch({"PRIME_AGENT_CODING_AGENT_DIR": str(acct.dir)}, args)


def _opencode(acct, cfg):
    acct.dir.mkdir(parents=True, exist_ok=True)
    if acct.mode == "isolated":
        return Launch({"XDG_DATA_HOME": str(acct.dir)}, [])
    auth = acct.dir / "opencode" / "auth.json"
    return Launch({"OPENCODE_AUTH_CONTENT": auth.read_text() if auth.exists() else "{}"}, [])


def _antigravity(acct, cfg):
    if acct.mode == "shared" and not AGY_PRIVATE:
        raise AdapterError('antigravity shared mode needs the token file name; set mode = "isolated" for now')
    real_home = Path(cfg.real_dirs["antigravity"])
    home = acct.dir / "home"
    with mirror.locked(f"antigravity-{acct.name}"):
        mirror.sync(real_home, home, skip={".gemini"})
        if acct.mode == "shared":
            mirror.sync(real_home / ".gemini", home / ".gemini", AGY_PRIVATE, seed=AGY_SEED)
        else:
            (home / ".gemini").mkdir(parents=True, exist_ok=True)
    sock = config.runtime_dir() / f"antigravity-{acct.name}.bus"
    fd = dbusproxy.start(os.environ["DBUS_SESSION_BUS_ADDRESS"], sock, cfg.bus_talk)
    return Launch({"HOME": str(home), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={sock}"}, [], (fd,))


def _ollama(acct, cfg):
    key = acct.dir / "key"
    if not logged_in(acct):
        raise NotLoggedIn(f"ollama/{acct.name}: no key; run ai-account login ollama {acct.name}")
    return Launch({"OLLAMA_API_KEY": key.read_text().strip()}, [])


PREPARE = {
    "claude": _claude,
    "prime-agent": _prime_agent,
    "opencode": _opencode,
    "antigravity": _antigravity,
    "ollama": _ollama,
}


def prepare(acct: Account, cfg: Config) -> Launch:
    if acct.is_default:
        return Launch({}, [])
    launch = PREPARE[acct.agent](acct, cfg)
    _apply_settings(acct, cfg, launch)
    return launch


def _apply_settings(acct: Account, cfg: Config, launch: Launch) -> None:
    rel = SETTINGS_FILE.get(acct.agent)
    ledger = acct.dir / LEDGER
    desired = settings.merge(cfg.managed.get(acct.agent, {}), acct.settings)
    if rel is None or (not desired and not ledger.exists()):
        return
    target = acct.dir / rel
    settings.apply(target, desired, ledger)
    if acct.agent == "opencode" and target.exists():
        launch.env["OPENCODE_CONFIG"] = str(target)


def creds_paths(acct: Account) -> list:
    if acct.agent == "antigravity":
        return [acct.dir / "home" / ".gemini" / p for p in AGY_CREDS]
    return [acct.dir / p for p in CREDS[acct.agent]]


def logged_in(acct: Account) -> bool:
    if acct.is_default:
        return True
    if acct.agent == "antigravity" and not AGY_CREDS:
        return (acct.dir / "home" / ".gemini" / "antigravity-cli").is_dir()
    paths = creds_paths(acct)
    return bool(paths) and all(p.is_file() and p.stat().st_size > 0 for p in paths)


def opencode_oauth(acct: Account) -> list:
    try:
        data = json.loads((acct.dir / "opencode" / "auth.json").read_text())
    except (FileNotFoundError, ValueError):
        return []
    return sorted(k for k, v in data.items() if isinstance(v, dict) and v.get("type") == "oauth")


def login_plan(acct: Account, cfg: Config):
    agent, bin_ = acct.agent, cfg.bins.get(acct.agent, "")
    if agent == "ollama":
        return None, {}, "paste the ollama.com API key for this account"
    if agent == "opencode":
        return [bin_, "auth", "login"], {"XDG_DATA_HOME": str(acct.dir)}, "pick the provider and finish its login"
    if agent == "antigravity":
        return [bin_], {}, "sign in when agy asks, then quit agy"
    return [bin_], {}, "run /login inside the agent, then quit it"
