"""prime-agent statusCommand helper: session cost, free-pool count, alimit rows and GPU power."""

import argparse
import calendar
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WATTMETER_NOW = Path("/run/wattmeter/now.json")
LEASE_DIR = Path("/run/wattmeter/ai")
FREE_WINDOW_S = 5 * 3600
FREE_TTL_S = 60
IP_TTL_S = 60
# A longer gap means no metered turn was sampling, so it adds no energy.
MAX_GAP_S = 5.0
FINE_S, COARSE_S = 60, 900
FINE_RETAIN_S, COARSE_RETAIN_S = 2 * 86400, 32 * 86400
TAIL_BYTES = 256 * 1024
MAX_LINES = 8
BAR_CELLS = 10
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def sgr(code, text):
    return f"\x1b[{code}m{text}\x1b[0m"


def dim(text):
    return sgr("2", text)


def severity(pct):
    return "31" if pct >= 80 else "33" if pct >= 50 else "32"


def bar(pct):
    filled = max(0, min(BAR_CELLS, int(pct / 100 * BAR_CELLS + 0.5)))
    return sgr(severity(pct), "▰" * filled) + dim("▱" * (BAR_CELLS - filled))


def fmt_num(v):
    """One decimal, dropped when .0. Truncated so 99.96 never reads as 100."""
    t = int(v * 10 + 1e-9) / 10
    return f"{t:.0f}" if t == int(t) else f"{t:.1f}"


def fmt_tokens(n):
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{fmt_num(n / 1000) if n < 100_000 else f'{n / 1000:.0f}'}k"
    return f"{fmt_num(n / 1_000_000)}M"


def fmt_cost(n):
    return f"${n:.4f}" if 0 < n < 1 else f"${n:.2f}"


def fmt_money(n, currency):
    return f"{currency}{n:.4f}" if 0 < n < 0.01 else f"{currency}{n:.2f}"


def fmt_dur(seconds):
    if seconds < 0:
        return None
    m = round(seconds / 60)
    if m < 1:
        return "<1m"
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h{m % 60}m"
    d, h = m // 1440, (m % 1440) // 60
    return f"{d}d{h}h" if h else f"{d}d"


def parse_ts(value):
    if isinstance(value, (int, float)):
        return value / 1000
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


# -- wattmeter service --------------------------------------------------------

def ai_reading(text, now_s):
    """The service's local-AI block, or None when unreadable or older than 3 intervals."""
    try:
        d = json.loads(text)
        if not now_s - d["ts"] <= d["interval"] * 3:
            return None
        ai = d.get("ai")
        if not isinstance(ai, dict):
            return None
        windows = [(str(w.get("label")), float(w.get("cost") or 0)) for w in ai.get("windows") or []]
        return {"currency": str(d.get("currency") or ""), "watts": float(ai.get("watts") or 0), "windows": windows}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def touch_lease(pid, lease_dir=LEASE_DIR):
    try:
        (Path(lease_dir) / f"prime-agent-{pid}").touch()
    except OSError:
        pass


def drop_lease(pid, lease_dir=LEASE_DIR):
    try:
        (Path(lease_dir) / f"prime-agent-{pid}").unlink()
    except OSError:
        pass


# -- hwmon fallback -----------------------------------------------------------

def find_sensor(card):
    cards = [card] if card else sorted(
        p.name for p in Path("/sys/class/drm").glob("card*") if re.fullmatch(r"card\d+", p.name))
    for name in cards:
        for path in sorted(Path(f"/sys/class/drm/{name}/device/hwmon").glob("*/power1_average")):
            return path
    return None


def read_watts(sensor):
    try:
        return int(sensor.read_text().strip()) / 1e6
    except (OSError, ValueError):
        return None


def load_energy(path):
    state = read_json(path, {})
    return {"last": float(state.get("last", 0)), "fine": dict(state.get("fine", {})),
            "coarse": dict(state.get("coarse", {}))}


def record_energy(path, watts, now_s):
    """Add watts times the gap since the last sample of any window; returns the new state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_energy(path)
        gap = now_s - state["last"]
        if watts is not None and 0 < gap <= MAX_GAP_S:
            for key, size, retain in (("fine", FINE_S, FINE_RETAIN_S), ("coarse", COARSE_S, COARSE_RETAIN_S)):
                buckets = state[key]
                slot = str(int(now_s // size))
                buckets[slot] = buckets.get(slot, 0) + watts * gap
                cutoff = int((now_s - retain) // size)
                for old in [k for k in buckets if int(k) < cutoff]:
                    del buckets[old]
        state["last"] = now_s
        write_json(path, state)
        return state


def parse_window(label):
    m = re.fullmatch(r"(\d+)([smhdwM])", label)
    return (int(m[1]), m[2]) if m else None


def window_start(now_s, n, unit):
    if unit != "M":
        return now_s - n * UNIT_S[unit]
    d = datetime.datetime.fromtimestamp(now_s)
    index = d.month - 1 - n
    year, month = d.year + index // 12, index % 12 + 1
    # Mar 31 minus one month is the end of February.
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day).timestamp()


def window_joules(state, now_s, n, unit):
    start = window_start(now_s, n, unit)
    fine = now_s - start <= FINE_RETAIN_S - FINE_S
    size, buckets = (FINE_S, state["fine"]) if fine else (COARSE_S, state["coarse"])
    # The bucket straddling the window start counts whole.
    cutoff = int(start // size)
    return sum(v for k, v in buckets.items() if int(k) >= cutoff)


def power_reading(power, payload, now_s, state_dir):
    """(watts or None, currency, [(label, cost)]) for a metered model."""
    active = bool(payload.get("turnActive"))
    try:
        ai = ai_reading(WATTMETER_NOW.read_text(), now_s)
    except OSError:
        ai = None
    if ai:
        # The service integrates while a fresh lease exists.
        if active:
            touch_lease(payload["pid"])
        else:
            drop_lease(payload["pid"])
        return (ai["watts"] if active else None), ai["currency"], ai["windows"]
    path = state_dir / "energy.json"
    sensor = find_sensor(power["card"]) if active else None
    watts = read_watts(sensor) if sensor else None
    marginal = None if watts is None else max(0.0, watts - power["idleWatts"])
    state = record_energy(path, marginal, now_s) if marginal is not None else load_energy(path)
    windows = []
    for label in power["windows"]:
        parsed = parse_window(label)
        if parsed:
            windows.append((label, window_joules(state, now_s, *parsed) / 3.6e6 * power["rateKwh"]))
    return marginal, power["currency"], windows


# -- session file -------------------------------------------------------------

def session_tokens(session_file, state_dir):
    """Input and output token totals of the session, reading only bytes added since last run."""
    path = state_dir / "tokens.json"
    cache = read_json(path, {})
    key = str(session_file)
    entry = cache.get(key, {"offset": 0, "input": 0, "output": 0})
    try:
        with open(session_file, "rb") as fh:
            if fh.seek(0, os.SEEK_END) < entry["offset"]:
                entry = {"offset": 0, "input": 0, "output": 0}
            fh.seek(entry["offset"])
            data = fh.read()
    except OSError:
        return 0, 0
    # A partial last line is read again next time.
    complete = data[: data.rfind(b"\n") + 1]
    for raw in complete.splitlines():
        try:
            message = json.loads(raw).get("message") or {}
        except ValueError:
            continue
        if message.get("role") == "assistant":
            usage = message.get("usage") or {}
            entry["input"] += int(usage.get("input") or 0)
            entry["output"] += int(usage.get("output") or 0)
    entry["offset"] += len(complete)
    write_json(path, {key: entry})
    return entry["input"], entry["output"]


def rate_limit_reset(session_file, now_s):
    """Reopen time of the newest 429 in the session tail, or None when passed or followed by a reply."""
    try:
        with open(session_file, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode(errors="replace").splitlines()
    except (OSError, TypeError):
        return None
    for raw in reversed(lines):
        try:
            message = json.loads(raw).get("message") or {}
        except ValueError:
            continue
        if message.get("role") != "assistant":
            continue
        for diag in message.get("diagnostics") or []:
            details = diag.get("details") or {}
            if diag.get("type") == "provider_stream_failure" and details.get("status") == 429:
                retry = details.get("retryAfterMs")
                if retry is None:
                    return None
                reset = parse_ts(diag.get("timestamp", 0)) + retry / 1000
                return reset if reset > now_s else None
        if message.get("stopReason") != "error":
            return None
    return None


# -- opencode free pools ------------------------------------------------------

def pool_of(model_id, pools):
    for name, models in pools.items():
        if model_id in models:
            return name, models
    return model_id, [model_id]


def is_free_model(provider, model_id, pools):
    in_pool = any(model_id in models for models in pools.values())
    return provider == "opencode" and (in_pool or model_id.endswith("-free"))


def count_pool_requests(sessions_dir, models, floor_s):
    count = 0
    for path in Path(sessions_dir).rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < floor_s:
                continue
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            if '"provider":"opencode"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") or {}
            ours = message.get("provider") == "opencode" and message.get("model") in models
            if ours and message.get("role") == "assistant" and parse_ts(entry.get("timestamp")) >= floor_s:
                count += 1
    return count


def ip_changed_at(state_dir, now_s):
    """When the egress IP last changed; the free buckets are per IP."""
    path = state_dir / "ip.json"
    state = read_json(path, {})
    if now_s - state.get("checkedAt", 0) >= IP_TTL_S:
        try:
            with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=2) as response:
                ip = json.load(response).get("ip")
        except (OSError, ValueError):
            ip = None
        if isinstance(ip, str):
            if state.get("ip") not in (None, ip):
                state["changedAt"] = now_s
            state["ip"] = ip
        state["checkedAt"] = now_s
        write_json(path, state)
    return state.get("changedAt", 0)


def sessions_dir(cfg, env):
    # An ai-accounts launch points prime-agent at the account's dir, same lookup as pa-daemon paths.rs.
    agent_dir = env.get("PRIME_AGENT_CODING_AGENT_DIR")
    return env.get("PRIME_AGENT_SESSION_DIR") or (f"{agent_dir}/sessions" if agent_dir else cfg["sessionsDir"])


def free_row(cfg, payload, model_id, now_s, state_dir):
    pool, models = pool_of(model_id, cfg["freePools"])
    changed = ip_changed_at(state_dir, now_s)
    path = state_dir / "free.json"
    cache = read_json(path, {})
    key = f"{cfg['sessionsDir']}:{pool}"
    hit = cache.get(key)
    if hit and now_s - hit["at"] < FREE_TTL_S and hit["changed"] == changed:
        count = hit["count"]
    else:
        count = count_pool_requests(cfg["sessionsDir"], models, max(now_s - FREE_WINDOW_S, changed))
        cache[key] = {"count": count, "at": now_s, "changed": changed}
        write_json(path, cache)
    cap = cfg["freeCaps"].get(pool, cfg["freeCapDefault"])
    pct = min(100, count / cap * 100)
    parts = [dim("reqs"), f"{count}/{cap}", bar(pct), sgr(severity(pct), f"{fmt_num(pct):>4}%")]
    reset = rate_limit_reset(payload.get("sessionFile"), now_s)
    if reset:
        parts.append(sgr("31", f"resets in {fmt_dur(reset - now_s) or 'soon'}"))
    return "  ".join(parts)


# -- alimit -------------------------------------------------------------------

def alimit_rows(provider):
    exe = shutil.which("alimit")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "bar", "--provider", provider], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = out.stdout.rstrip()
    # An empty DB prints a box saying "alimit: no usage"; hide it.
    if out.returncode != 0 or not text or "alimit: no usage" in text:
        return []
    return text.split("\n")


# -- extra rows ---------------------------------------------------------------

def extra_rows(command):
    try:
        out = subprocess.run([command], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = out.stdout.rstrip()
    if out.returncode != 0 or not text:
        return []
    return text.split("\n")


# -- render -------------------------------------------------------------------

def cost_line(payload, tokens, free, power):
    if power:
        watts, currency, windows = power
        parts = [sgr("33", "⚡")]
    else:
        cost = payload.get("costUsd") or 0
        parts = [sgr("36", "◆"), dim("free") if free else sgr("1", fmt_cost(cost))]
    parts += [f"↑{fmt_tokens(tokens[0])}", f"↓{fmt_tokens(tokens[1])}"]
    if power:
        if watts is not None:
            parts.append(f"{watts:.0f}W")
        if windows:
            parts.append(dim("·"))
            parts += [f"{dim(label)} {fmt_money(cost, currency)}" for label, cost in windows]
    return " ".join(parts)


def render(cfg, payload, now_s, state_dir):
    model = payload.get("model") or {}
    provider, model_id = model.get("provider", ""), model.get("id", "")
    power = cfg.get("power")
    metered = bool(power) and provider in power["providers"]
    if power and not metered:
        drop_lease(payload["pid"])
    free = is_free_model(provider, model_id, cfg["freePools"])
    lines = []
    if cfg["showCost"]:
        session_file = payload.get("sessionFile")
        tokens = session_tokens(session_file, state_dir) if session_file else (0, 0)
        reading = power_reading(power, payload, now_s, state_dir) if metered else None
        lines.append(cost_line(payload, tokens, free, reading))
        if free:
            lines.append(free_row(cfg, payload, model_id, now_s, state_dir))
    alimit_provider = cfg["alimitProviders"].get(provider)
    if cfg["alimit"] and alimit_provider:
        lines += alimit_rows(alimit_provider)
    for command in cfg.get("extraRows", []):
        lines += extra_rows(command)
    return lines[:MAX_LINES]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="prime-agent-status")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.read_text())
    cfg["sessionsDir"] = sessions_dir(cfg, os.environ)
    payload = json.load(sys.stdin)
    state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "prime-agent-status"
    for row in render(cfg, payload, time.time(), state_dir):
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
