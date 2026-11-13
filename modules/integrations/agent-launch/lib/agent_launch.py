"""agent-launch: pick one of the configured coding agents with fzf and start it."""

import argparse
import json
import math
import os
import secrets
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
    return proc.stdout.rstrip("\r\n") if proc.returncode == 0 else None


def _pct(value, decimals: bool = False) -> str:
    if value is None:
        return "\u2014"
    t = math.floor(value * 10 + 1e-9) / 10
    if decimals:
        return f"{t:.1f}%"
    formatted = f"{t:.0f}" if t == int(t) else f"{t:.1f}"
    return f"{formatted}%"


def _account_columns(rows: list) -> tuple[list[str], dict[str, tuple[int, bool]]]:
    all_keys = []
    for r in rows:
        for k in (r.get("windows") or {}):
            if k not in all_keys:
                all_keys.append(k)
    columns = [
        k for k in all_keys
        if any((v := (r.get("windows") or {}).get(k)) is not None and v > 0 for r in rows)
    ]
    pct_specs = {}
    for k in columns:
        vals = [(v := (r.get("windows") or {}).get(k)) for r in rows if (v := (r.get("windows") or {}).get(k)) is not None and v > 0]
        has_decimals = any((math.floor(v * 10 + 1e-9) / 10) != int(math.floor(v * 10 + 1e-9) / 10) for v in vals)
        pw = max(len(_pct(v, decimals=has_decimals)) for v in vals) if vals else 0
        pct_specs[k] = (pw, has_decimals)
    return columns, pct_specs


def _account_line(r: dict, width: int = 12, columns: list[str] | None = None,
                  pct_widths: dict | None = None) -> str:
    tail = " (auto)" if r.get("next") else " (sticky)" if r.get("sticky") else ""
    login = "" if r.get("loggedIn") else "  needs login"
    if columns is None:
        columns, pct_widths = _account_columns([r])
    if columns:
        cells = []
        for k in columns:
            spec = (pct_widths or {}).get(k, 0)
            pw, dec = spec if isinstance(spec, tuple) else (spec, False)
            val = (r.get("windows") or {}).get(k)
            if val is not None and val > 0:
                cells.append(f"{k} {_pct(val, decimals=dec):>{pw}}")
            else:
                cells.append(f"{k} {'-':>{pw}}")
        body = "  ".join(cells)
    else:
        body = _pct(r.get("maxUtil"))
    return f"{r['label']:<{width}} {body}{tail}{login}".rstrip()


def _account_lines(rows: list) -> list[str]:
    width = max((len(r.get("label", "")) for r in rows), default=12)
    width = max(12, width)
    columns, pct_widths = _account_columns(rows)
    return [_account_line(r, width=width, columns=columns, pct_widths=pct_widths) for r in rows]


def pick_account(name: str, lister=None, picker=None) -> str | None:
    rows = (lister or list_accounts)(name)
    if len(rows) < 2:
        return None
    lines = _account_lines(rows)
    chosen = (picker or fzf_account)(lines)
    return rows[lines.index(chosen)]["account"] if chosen in lines else None


def choose(cfg: dict, picker=None, agent: str | None = None) -> tuple[str | None, list[str] | None]:
    agents = cfg.get("agents", {})
    default = cfg.get("defaultAgent", "")
    if agent:
        name = agent if agent in agents else None
    else:
        name = default if default in agents else (picker or pick_agent)(cfg)
    if name is None:
        return None, None
    return name, list(agents.get(name, {}).get("command") or [name])


def relay_env(cfg: dict, gen=None) -> dict:
    """AGENT_RELAY_ID for agent-relay, so the ID is known before the agent starts."""
    if not cfg.get("relayIds"):
        return {}
    return {"AGENT_RELAY_ID": (gen or (lambda: secrets.token_hex(3)[:5]))()}


def relay_title(name: str, rid: str) -> str:
    return f"\033]0;{name} · {rid}\007"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-launch")
    parser.add_argument("--config", required=True)
    # For wrappers such as zed-agent-bridge that start the agent themselves.
    parser.add_argument("--print", action="store_true", help="print the choice as JSON instead of starting it")
    parser.add_argument("--agent", help="start this agent without the picker")
    parser.add_argument("--auto-account", action="store_true",
                        help="skip the account picker; ai-account picks the sticky or auto account")
    parser.add_argument("args", nargs="*", help="extra arguments for the agent, after --")
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text())
    name, cmd = choose(cfg, agent=args.agent)
    if not name or not cmd:
        if args.agent:
            print(f"agent-launch: unknown agent {args.agent!r}; valid: {', '.join(cfg.get('agents', {}))}", file=sys.stderr)
        return 1
    cmd += args.args
    account = None if args.auto_account else pick_account(name)
    env = {"AI_ACCOUNT_" + name.upper().replace("-", "_"): account} if account else {}
    env |= relay_env(cfg)
    if args.print:
        print(json.dumps({"agent": name, "argv": cmd, "account": account, "env": env}), flush=True)
        return 0
    if rid := env.get("AGENT_RELAY_ID"):
        sys.stdout.write(relay_title(name, rid) + f"relay ID {rid}\n")
        sys.stdout.flush()
    os.environ.update(env)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
