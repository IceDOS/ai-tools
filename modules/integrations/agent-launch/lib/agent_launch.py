"""agent-launch: pick one of the configured coding agents with fzf and start it."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def label(name: str, agent: dict) -> str:
    return agent.get("label") or name.replace("-", " ").title()


def pick_agent(cfg: dict) -> str | None:
    agents = cfg.get("agents", {})
    if len(agents) == 1:
        return next(iter(agents))
    if not sys.stdin.isatty():
        return "claude" if "claude" in agents else next(iter(agents), None)

    labels = {label(name, agent): name for name, agent in agents.items()}
    try:
        proc = subprocess.run(
            ["fzf", "--height=60%", "--layout=reverse", "--border", "--prompt=agent> "],
            input="\n".join(labels),
            text=True,
            stdout=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return next(iter(agents), None)
    if proc.returncode == 0 and proc.stdout.strip() in labels:
        return labels[proc.stdout.strip()]
    return None


def list_accounts(name: str) -> list:
    try:
        proc = subprocess.run(["ai-account", "list", "--agent", name, "--json"],
                              capture_output=True, text=True, check=False, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    try:
        return json.loads(proc.stdout).get(name, []) if proc.returncode == 0 else []
    except ValueError:
        return []


def fzf_account(lines: list) -> str | None:
    if not sys.stdin.isatty():
        return None
    proc = subprocess.run(["fzf", "--height=40%", "--layout=reverse", "--border", "--prompt=account> "],
                          input="\n".join(lines), text=True, stdout=subprocess.PIPE, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _pct(value) -> str:
    return "\u2014" if value is None else f"{value:.0f}%"


def _account_line(r: dict) -> str:
    tail = " (auto)" if r.get("next") else " (sticky)" if r.get("sticky") else ""
    login = "" if r.get("loggedIn") else "  needs login"
    windows = "  ".join(f"{k} {_pct(v):>4}" for k, v in (r.get("windows") or {}).items())
    return f"{r['label']:<12} {windows or _pct(r.get('maxUtil')):>4}{tail}{login}"


def pick_account(name: str, lister=None, picker=None) -> str | None:
    rows = (lister or list_accounts)(name)
    if len(rows) < 2:
        return None
    lines = [_account_line(r) for r in rows]
    chosen = (picker or fzf_account)(lines)
    return rows[lines.index(chosen)]["account"] if chosen in lines else None


def choose(cfg: dict, picker=None) -> tuple[str | None, list[str] | None]:
    agents = cfg.get("agents", {})
    default = cfg.get("defaultAgent", "")
    name = default if default in agents else (picker or pick_agent)(cfg)
    if name is None:
        return None, None
    return name, list(agents.get(name, {}).get("command") or [name])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-launch")
    parser.add_argument("--config", required=True)
    # For wrappers such as zed-agent-bridge that start the agent themselves.
    parser.add_argument("--print", action="store_true", help="print the choice as JSON instead of starting it")
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text())
    name, cmd = choose(cfg)
    if not name or not cmd:
        return 1
    account = pick_account(name)
    env = {"AI_ACCOUNT_" + name.upper().replace("-", "_"): account} if account else {}
    if args.print:
        print(json.dumps({"agent": name, "argv": cmd, "account": account, "env": env}), flush=True)
        return 0
    os.environ.update(env)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
