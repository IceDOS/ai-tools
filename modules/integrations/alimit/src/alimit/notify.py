"""Optional desktop alerts via notify-send, debounced per window through the meta table.

Plasma never expires critical urgency, so AlertStyle defaults to non-sticky.
"""
import dataclasses
import shutil
import subprocess
import time

from . import config, store

THRESHOLDS = (80.0, 95.0)
DEBOUNCE_MS = 30 * 60_000


@dataclasses.dataclass(frozen=True)
class AlertStyle:
    urgency: str = "normal"  # low | normal | critical
    timeout: int = 10  # seconds on screen; 0 = until dismissed
    transient: bool = False  # skip the notification history


DEFAULT_STYLE = AlertStyle()


def _send(con, key: str, title: str, body: str, style: AlertStyle = DEFAULT_STYLE) -> None:
    exe = shutil.which("notify-send")
    if not exe:
        return
    argv = [exe, "-a", "alimit", "-u", style.urgency, "-t", str(style.timeout * 1000), "-p"]
    if style.transient:
        argv += ["-h", "int:transient:1"]
    nid = store.get_meta_int(con, key + "_nid", 0)
    if nid:
        argv += ["-r", str(nid)]  # a stale id is harmless — the server just makes a new one
    argv += [title, body]
    try:
        proc = subprocess.run(argv, check=False, timeout=10, capture_output=True, text=True)
        store.set_meta(con, key + "_nid", int((proc.stdout or "").strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def _condition(r) -> str | None:
    if r.util >= 100.0:
        return "full"
    if r.will_exhaust_before_reset and r.per_hour >= 0.05:  # ~0.0%/h is no real pace
        return "exhaust"
    for t in sorted(THRESHOLDS, reverse=True):
        if r.util >= t:
            return f"th{int(t)}"
    return None


def check(con, rlist, now_ms: int | None = None, style: AlertStyle = DEFAULT_STYLE) -> None:
    """Alert on the windows every surface shows (cli.collect's output)."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    for r in rlist:
        cond = _condition(r)
        key = f"alert_{r.window}"
        if cond is None:
            # cleared → a re-cross re-notifies; _nid kept so it replaces rather than stacks
            store.set_meta(con, key, "")
            continue
        prev = store.get_meta(con, key, "")
        if prev:
            # a full window can't go further; otherwise repeat only after new usage
            if prev == "full":
                continue
            if r.util <= float(store.get_meta(con, key + "_util", "0") or 0):
                continue
            if prev == cond and now - store.get_meta_int(con, key + "_ms", 0) < DEBOUNCE_MS:
                continue
        store.set_meta(con, key, cond)
        store.set_meta(con, key + "_ms", now)
        store.set_meta(con, key + "_util", r.util)
        label = config.label(r.window)
        if config.provider_of(r.window) == "claude":
            label = "Claude · " + label  # other providers already carry their name
        if cond == "exhaust":
            _send(
                con,
                key,
                "alimit — pace warning",
                f"{label}: {r.util:.0f}% used, ~{r.per_hour:.1f}%/h — projected to hit the cap "
                "before it resets.",
                style,
            )
        elif cond == "full":
            _send(con, key, "alimit — limit reached", f"{label}: 100% used.", style)
        else:
            _send(con, key, "alimit — usage high", f"{label}: {r.util:.0f}% used.", style)
