"""Antigravity CLI status line: alimit's shared agent bar, plus the terminal title.

alimit renders the title row (dir, git, model, context) and the quota table, and records agy's
quota buckets in its usage DB, so the same windows reach `alimit`, its widget and its alerts.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SETTINGS = Path.home() / ".gemini/antigravity-cli/settings.json"
ALIMIT = os.environ.get("ANTIGRAVITY_ALIMIT", "alimit")


def render(payload):
    try:
        proc = subprocess.run(
            [ALIMIT, "statusline", "--agent", "antigravity"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"alimit unavailable: {e}"
    return proc.stdout.rstrip("\n") or proc.stderr.strip()


def read_payload():
    if sys.stdin.isatty():
        return {}
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def setup(action, command, stack):
    """Merge/remove the statusLine block in agy's own settings file (it writes that file)."""
    data = None
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            print(f"antigravity statusline: {SETTINGS} is not valid JSON; left untouched", file=sys.stderr)
            return 0
        if not isinstance(data, dict):
            data = None
    if action == "off":
        if data is None or "statusLine" not in data:
            return 0
        del data["statusLine"]
    else:
        desired = {"type": "command", "command": command}
        if stack:
            desired["stack_with_default"] = True
        # agy owns this file; skip the rewrite when only the mtime would change.
        if data is not None and data.get("statusLine") == desired:
            return 0
        data = {} if data is None else data
        data["statusLine"] = desired
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, SETTINGS)
    return 0


def main(argv):
    if argv and argv[0] == "--setup":
        stack = "--stack" in argv
        args = [a for a in argv[1:] if a != "--stack"]
        action = args[0] if args else "on"
        command = args[1] if len(args) > 1 else str(SETTINGS.parent / "statusline.sh")
        return setup(action, command, stack)
    payload = read_payload()
    if "--title" in argv:
        try:
            import terminal_title

            terminal_title.update(payload)
        except Exception:  # noqa: BLE001, S110 - the title must never break the bar
            pass
    # --no-bar keeps agy's default bar (stacked) while the call still drives the title
    if "--no-bar" not in argv:
        print(render(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
