"""Usage windows from providers other than Claude, in sources.py's output shape.

Each poller reads a key another agent already stores and returns
{"<provider>/<window>": {"util", "resets_at"}}. Antigravity has no key alimit can
read (agy keeps its login in the system keyring), so its windows arrive through
`alimit record antigravity`, piped agy's status line payload.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config
from .sources import FetchError


class NoCredentials(Exception):
    pass


class NotSubscribed(Exception):
    pass


def _window(provider: str, part: str) -> str:
    return provider + config.PROVIDER_SEP + part


def _read_keys(path, names):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for n in names:
        entry = data.get(n) if isinstance(data, dict) else None
        key = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key:
            out.append(key)
    return out


def _keys(env: str, prime_names=(), opencode_names=()):
    """Candidate keys in order: env var, prime-agent's auth.json, opencode's auth.json."""
    keys = [os.environ[env]] if os.environ.get(env) else []
    keys += _read_keys(config.PRIME_AGENT_AUTH, prime_names)
    keys += _read_keys(config.OPENCODE_AUTH, opencode_names)
    # dedupe, keep order
    return list(dict.fromkeys(keys))


def _get(url: str, key: str):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "alimit"},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = None
        raise FetchError(f"HTTP {e.code}", status=e.code, body=body) from e
    except urllib.error.URLError as e:
        raise FetchError(f"fetch failed: {e.reason}") from e
    except ValueError as e:
        raise FetchError(f"bad JSON: {e}") from e


def _first_working(url: str, keys):
    """GET with each key until one is accepted. Keys can belong to different accounts,
    so a 401 or 403 tries the next one; the most telling error is raised at the end."""
    if not keys:
        raise NoCredentials("no API key found")
    rejected, other = None, None
    for key in keys:
        try:
            return _get(url, key)
        except FetchError as e:
            if e.status == 401:
                rejected = e
            elif e.status == 403:
                other = other or e
            else:
                raise
    if other:
        raise other
    raise NoCredentials(f"every key was rejected ({rejected})")


def _pct(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return None


# ---------- OpenCode Go ----------
def parse_opencode_go(data) -> dict:
    usage = data.get("usage") if isinstance(data, dict) else None
    out = {}
    for part in ("rolling", "weekly", "monthly"):
        w = usage.get(part) if isinstance(usage, dict) else None
        if not isinstance(w, dict):
            continue
        util = _pct(w.get("percent"))
        if util is not None:
            out[_window("opencode-go", part)] = {"util": util, "resets_at": w.get("resetsAt")}
    return out


def fetch_opencode_go() -> dict:
    keys = _keys("OPENCODE_API_KEY", ("opencode-go", "opencode"), ("opencode-go", "opencode"))
    try:
        return parse_opencode_go(_first_working(config.OPENCODE_GO_USAGE_URL, keys))
    except FetchError as e:
        err = (e.body if isinstance(e.body, dict) else {}).get("error") or {}
        if isinstance(err, dict) and err.get("type") == "EntitlementError":
            raise NotSubscribed("no OpenCode Go subscription") from e
        raise


# ---------- Ollama ----------
def parse_ollama(data) -> dict:
    included = data.get("included") if isinstance(data, dict) else None
    if not isinstance(included, dict):
        return {}
    out = {}
    for part in ("session", "weekly"):
        w = included.get(part)
        if not isinstance(w, dict):
            continue
        left = _pct(w.get("remaining_percent"))
        if left is not None:
            out[_window("ollama", part)] = {"util": 100.0 - left, "resets_at": w.get("resets_at")}
    allowance, balance = included.get("allowance_usd"), included.get("balance_usd")
    if isinstance(allowance, (int, float)) and allowance > 0 and isinstance(balance, (int, float)):
        period = included.get("period") or {}
        out[_window("ollama", "allowance")] = {
            "util": _pct((allowance - balance) / allowance * 100),
            "resets_at": period.get("until") if isinstance(period, dict) else None,
        }
    return out


def fetch_ollama() -> dict:
    return parse_ollama(_first_working(config.OLLAMA_BALANCE_URL, _keys("OLLAMA_API_KEY", ("ollama",))))


# ---------- OpenRouter ----------
def _next_reset(kind, now: datetime):
    """Next UTC boundary for OpenRouter's limit_reset (daily/weekly/monthly), as ISO."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        nxt = day + timedelta(days=1)
    elif kind == "weekly":
        nxt = day + timedelta(days=7 - day.weekday())
    elif kind == "monthly":
        nxt = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        return None
    return nxt.isoformat()


def parse_openrouter(data, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return {}
    out = {}
    free = d.get("free_model_daily_requests")
    if isinstance(free, dict) and isinstance(free.get("limit"), (int, float)) and free["limit"] > 0:
        out[_window("openrouter", "free_daily")] = {
            "util": _pct(100.0 * (free.get("used") or 0) / free["limit"]),
            "resets_at": _next_reset("daily", now),
        }
    limit, left = d.get("limit"), d.get("limit_remaining")
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(left, (int, float)):
        out[_window("openrouter", "credits")] = {
            "util": _pct(100.0 * (limit - left) / limit),
            "resets_at": _next_reset(d.get("limit_reset"), now),
        }
    return out


def fetch_openrouter() -> dict:
    return parse_openrouter(_first_working(config.OPENROUTER_KEY_URL, _keys("OPENROUTER_API_KEY", ("openrouter",))))


# ---------- Antigravity (status line payload) ----------
def parse_antigravity(payload, now_ms: int) -> dict:
    """agy's status line `quota` buckets: {"gemini-5h": {remaining_fraction, reset_in_seconds}}."""
    quota = payload.get("quota") if isinstance(payload, dict) else None
    out = {}
    for key, q in (quota or {}).items():
        if not isinstance(q, dict) or not isinstance(q.get("remaining_fraction"), (int, float)):
            continue
        reset = q.get("reset_in_seconds")
        resets_at = (
            # reset_in_seconds jitters every redraw; whole minutes keep repeat rows out of the DB
            datetime.fromtimestamp(round((now_ms / 1000 + reset) / 60) * 60, tz=timezone.utc).isoformat()
            if isinstance(reset, (int, float)) and not isinstance(reset, bool)
            else None
        )
        out[_window("antigravity", key)] = {
            "util": _pct((1 - q["remaining_fraction"]) * 100),
            "resets_at": resets_at,
        }
    return out


# Providers the timer polls; antigravity is fed by its status line instead.
POLLERS = {
    "opencode-go": fetch_opencode_go,
    "ollama": fetch_ollama,
    "openrouter": fetch_openrouter,
}
PAYLOAD_PARSERS = {"antigravity": parse_antigravity}
