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
    if args.print:
        print(json.dumps({"agent": name, "argv": cmd}), flush=True)
        return 0
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
