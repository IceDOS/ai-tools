"""Antigravity CLI status line: alimit's shared agent bar, plus the terminal title.

alimit renders the title row (dir, git, model, context) and the quota table, and records agy's
quota buckets in its usage DB, so the same windows reach `alimit`, its widget and its alerts.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ALIMIT = os.environ.get("ANTIGRAVITY_ALIMIT", "alimit")
CACHE_DIR = Path.home() / ".gemini/antigravity-cli/cache"
QUOTA_CACHE = CACHE_DIR / "quota_cache.json"
QUOTA_LOCK = CACHE_DIR / "quota_fetch.lock"


def find_agy_bin():
    return os.environ.get("ANTIGRAVITY_BIN") or shutil.which("agy")


def fetch_quota(agy_bin=None):
    if os.environ.get("ANTIGRAVITY_QUOTA_FETCH"):
        return 0
    agy = agy_bin or find_agy_bin()
    if not agy:
        return 1
    env = dict(os.environ)
    env["ANTIGRAVITY_QUOTA_FETCH"] = "1"
    try:
        proc = subprocess.run(
            [agy, "--output-format", "json", "--settings", '{"statusLine":{"type":"none"}}', "-p", "/quota"],
            capture_output=True, text=True, timeout=15, env=env, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return 1
        data = json.loads(proc.stdout)
    except Exception:
        return 1

    quota = {}
    now = time.time()
    for g in data.get("command", {}).get("data", {}).get("groups", []):
        for b in g.get("buckets", []):
            bid = b.get("id")
            rem = b.get("remaining_fraction")
            reset_time = b.get("reset_time")
            if bid and isinstance(rem, (int, float)):
                reset_sec = None
                if isinstance(reset_time, str):
                    try:
                        from datetime import datetime

                        dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                        reset_sec = max(0.0, dt.timestamp() - now)
                    except Exception:
                        pass
                quota[bid] = {"remaining_fraction": rem, "reset_in_seconds": reset_sec}
    if not quota:
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUOTA_CACHE.with_name(QUOTA_CACHE.name + ".tmp")
    tmp.write_text(json.dumps({"ts": now, "quota": quota}))
    os.replace(tmp, QUOTA_CACHE)

    try:
        subprocess.run(
            [ALIMIT, "record", "antigravity"],
            input=json.dumps({"quota": quota}), capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        pass
    return 0


def maybe_refresh_quota(payload):
    if os.environ.get("ANTIGRAVITY_QUOTA_FETCH"):
        return
    now = time.time()
    cached = None
    if QUOTA_CACHE.exists():
        try:
            cached = json.loads(QUOTA_CACHE.read_text())
        except Exception:
            pass
    if cached and isinstance(cached.get("quota"), dict):
        payload.setdefault("quota", {})
        payload["quota"].update(cached["quota"])

    last_fetch = 0.0
    if QUOTA_LOCK.exists():
        try:
            last_fetch = QUOTA_LOCK.stat().st_mtime
        except OSError:
            pass
    elif cached and isinstance(cached.get("ts"), (int, float)):
        last_fetch = cached["ts"]

    if now - last_fetch >= 60:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            QUOTA_LOCK.touch(exist_ok=True)
            agy = find_agy_bin()
            args = [sys.executable, str(Path(__file__).resolve()), "--fetch-quota"]
            if agy:
                args += ["--agy", agy]
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass


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


def main(argv):
    if argv and argv[0] == "--fetch-quota":
        agy = argv[argv.index("--agy") + 1] if "--agy" in argv and len(argv) > argv.index("--agy") + 1 else None
        return fetch_quota(agy)
    payload = read_payload()
    maybe_refresh_quota(payload)
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
