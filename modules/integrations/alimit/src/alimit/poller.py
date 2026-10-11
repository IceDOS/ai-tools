"""One live acquisition, run by the systemd timer (or `alimit poll`).

The rate-limit floor and 429 backoff live in the meta table, so every caller
shares them and no caller can trip the endpoint's 429 bucket.
"""
import time

from . import auth, config, sources, store

# Timer activations land a few ms either side of the interval; don't skip those.
FLOOR_SLACK_MS = 5_000
MAX_BACKOFF_S = 3600


def _meta(name: str, tag: str) -> str:
    """Scope a meta key to an account: ollama_next_ms -> ollama@home_next_ms."""
    return name if tag == "default" else f"{name.split('_', 1)[0]}@{tag}_{name.split('_', 1)[1]}"


def poll_once(con, now_ms: int | None = None, creds=None, tag: str = "default"):
    """Returns (status, rows_inserted); status is 'poll', 'throttled', 'skipped: …' or 'error: …'."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    live_next, live_backoff = _meta("live_next_ms", tag), _meta("live_backoff_s", tag)
    if now < store.get_meta_int(con, live_next, 0) - FLOOR_SLACK_MS:
        return "throttled", 0

    try:
        token = auth.get_access_token(creds) if creds else auth.get_access_token()
    except auth.AuthError as e:
        return f"skipped: {e}", 0

    # Claim the slot before fetching so a concurrent caller sees the floor.
    store.set_meta(con, live_next, now + config.MIN_INTERVAL * 1000)
    try:
        windows = sources.fetch_live(token)
    except sources.FetchError as e:
        if e.status == 429:
            backoff = min(max(store.get_meta_int(con, live_backoff) * 2, config.MIN_INTERVAL), MAX_BACKOFF_S)
            store.set_meta(con, live_backoff, backoff)
            store.set_meta(con, live_next, now + backoff * 1000)
        return f"error: {e}", 0

    store.set_meta(con, live_backoff, 0)
    windows = {config.with_account(k, tag): w for k, w in windows.items()}
    return "poll", store.record(con, now, windows, "poll")


# A provider without a key or plan is checked again after this, not every tick.
IDLE_RECHECK_S = 6 * 3600


def poll_provider(con, provider: str, now_ms: int | None = None, keys=None, tag: str = "default"):
    """poll_once for one providers.POLLERS entry, with its own floor and backoff in meta."""
    from . import providers

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    next_key, backoff_key = _meta(f"{provider}_next_ms", tag), _meta(f"{provider}_backoff_s", tag)
    if now < store.get_meta_int(con, next_key, 0) - FLOOR_SLACK_MS:
        return "throttled", 0

    store.set_meta(con, next_key, now + config.MIN_INTERVAL * 1000)
    try:
        windows = providers.POLLERS[provider](keys=keys) if keys else providers.POLLERS[provider]()
    except (providers.NoCredentials, providers.NotSubscribed) as e:
        store.set_meta(con, next_key, now + IDLE_RECHECK_S * 1000)
        return f"skipped: {e}", 0
    except sources.FetchError as e:
        backoff = min(max(store.get_meta_int(con, backoff_key) * 2, config.MIN_INTERVAL), MAX_BACKOFF_S)
        store.set_meta(con, backoff_key, backoff)
        store.set_meta(con, next_key, now + backoff * 1000)
        return f"error: {e}", 0

    store.set_meta(con, backoff_key, 0)
    windows = {config.with_account(k, tag): w for k, w in windows.items()}
    return "poll", store.record(con, now, windows, "poll")


def poll_all(con, now_ms: int | None = None):
    """Claude plus every enabled provider poller: {name: (status, rows)}."""
    from . import providers

    enabled = config.ENABLED_PROVIDERS
    out = {}
    if not enabled or "claude" in enabled:
        out["claude"] = poll_once(con, now_ms)
    for name in providers.POLLERS:
        if not enabled or name in enabled:
            out[name] = poll_provider(con, name, now_ms)
    from . import accounts

    for acct in accounts.declared():
        if acct.agent == "claude":
            if not enabled or "claude" in enabled:
                out[f"claude@{acct.tag}"] = poll_once(con, now_ms, creds=acct.credentials, tag=acct.tag)
        elif acct.agent == "ollama":
            key = acct.credentials.read_text().strip() if acct.credentials.exists() else ""
            if key and (not enabled or "ollama" in enabled):
                out[f"ollama@{acct.tag}"] = poll_provider(con, "ollama", now_ms, keys=[key], tag=acct.tag)
        elif acct.agent in config.MULTI_PROVIDER_AGENTS:
            for provider in config.AGENT_PROVIDERS[acct.agent]:
                if enabled and provider not in enabled:
                    continue
                keys = providers._read_keys(acct.credentials, providers.KEY_NAMES[provider])
                if keys:
                    out[f"{provider}@{acct.tag}"] = poll_provider(con, provider, now_ms, keys=keys, tag=acct.tag)
    return out
