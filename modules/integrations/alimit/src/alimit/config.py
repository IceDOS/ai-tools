"""Paths, endpoints, and tunables. Everything overridable via env."""
import os
from pathlib import Path

HOME = Path.home()


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


# --- credentials (Claude Code's own file, read-only) ---
CREDS_PATH = _env_path("ALIMIT_CREDS", HOME / ".claude" / ".credentials.json")

# Claude Code settings, read for autoCompactWindow (the status line's context denominator).
SETTINGS_PATH = _env_path("CLAUDE_CONFIG_DIR", HOME / ".claude") / "settings.json"

# --- our storage (XDG) ---
_DATA_HOME = _env_path("XDG_DATA_HOME", HOME / ".local" / "share")
DATA_DIR = _DATA_HOME / "alimit"
DB_PATH = _env_path("ALIMIT_DB", DATA_DIR / "usage.db")
# Before the rename to alimit; store.connect moves it over once.
LEGACY_DB_PATH = _DATA_HOME / "climit" / "usage.db"

# --- Anthropic OAuth usage endpoint ---
API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
USAGE_PATH = "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
# UA prefix "claude-code/" is REQUIRED — without it the endpoint drops you into
# an aggressively rate-limited bucket (persistent 429). Version is cosmetic.
USER_AGENT = os.environ.get("ALIMIT_UA", "claude-code/2.1.212")

# --- polling ---
MIN_INTERVAL = 180          # hard floor between live fetches (429 safety)
HTTP_TIMEOUT = 30

# Samples read per window: a full weekly window plus a day of slack for its reset.
HISTORY_MS = 8 * 86_400_000

# --- windows: stable keys present in BOTH cache and live responses ---
WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "seven_day_opus": "week · Opus",
    "seven_day_sonnet": "week · Sonnet",
}
WINDOW_ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]

# Real burn-window keys; internal model codenames (nimbus_quill, ...) ignored, mirroring Claude Code's allowlist.
# Tuple copy so later mutation of WINDOW_ORDER can't silently diverge the set.
KNOWN_WINDOW_KEYS = tuple(WINDOW_ORDER)

# Per-model weekly windows are synthesized from limits[] (kind == "weekly_scoped")
# as "<WEEKLY_SCOPED_PREFIX><slug>", so they flow through the same samples table.
WEEKLY_SCOPED_PREFIX = "weekly_scoped:"

# Retire weekly_scoped windows with no sample for this many days (0 = disabled).
# Active windows are re-recorded every poll (resets_at jitters), so this never hides them.
WEEKLY_SCOPED_RETIRE_DAYS = int(os.environ.get("ALIMIT_SCOPED_RETIRE_DAYS", "30"))

# Shorthand labels for compact surfaces (statusline, panel widget).
SHORT_LABELS = {
    "five_hour": "5h",
    "seven_day": "wk",
    "seven_day_opus": "opus",
    "seven_day_sonnet": "son",
}


def weekly_scoped_slug(display_name: str) -> str:
    """Stable slug for a per-model window key: lowercase, spaces -> '_'. Other
    punctuation ('.', '-') is preserved so names like 'Claude Opus 4.5' round-trip."""
    return display_name.strip().lower().replace(" ", "_")


def window_display_name(window: str) -> str:
    """Inverse of weekly_scoped_slug: 'weekly_scoped:claude_opus_4.5' -> 'Claude Opus 4.5'."""
    slug = window.removeprefix(WEEKLY_SCOPED_PREFIX)
    return " ".join(w.title() for w in slug.split("_"))


def _is_known_base(window: str) -> bool:
    """True for windows alimit should render: the stable dict windows plus any
    synthesized weekly_scoped key. Keeps phantom/promotional keys (incl. leftover
    historical DB rows) out of every surface."""
    return (
        window in KNOWN_WINDOW_KEYS
        or window.startswith(WEEKLY_SCOPED_PREFIX)
        or provider_of(window) != "claude"
    )


def is_known_window(window: str) -> bool:
    return _is_known_base(split_account(window)[0])


def _base_label(window: str) -> str:
    if provider_of(window) != "claude":
        provider, family, part = _provider_parts(window)
        name = PROVIDER_WINDOWS[part][0] if part in PROVIDER_WINDOWS else part
        return " · ".join(p for p in (PROVIDERS[provider], family and family[0], name) if p)
    if window in WINDOW_LABELS:
        return WINDOW_LABELS[window]
    if window.startswith(WEEKLY_SCOPED_PREFIX):
        return "week · " + window_display_name(window)
    return window


def label(window: str) -> str:
    base, tag = split_account(window)
    text = _base_label(base)
    return text if tag == "default" else f"{text} · {tag}"


def _base_short_label(window: str) -> str:
    if provider_of(window) != "claude":
        provider, family, part = _provider_parts(window)
        name = PROVIDER_WINDOWS[part][1] if part in PROVIDER_WINDOWS else part[:3]
        return " ".join(p for p in (PROVIDER_SHORT[provider], family and family[1], name) if p)
    if window in SHORT_LABELS:
        return SHORT_LABELS[window]
    if window.startswith(WEEKLY_SCOPED_PREFIX):
        # Drop "Claude " prefix so families don't collapse to "Cla", append version for distinctness (e.g. Opus 4.1/4.5).
        words = window_display_name(window).split()
        if len(words) > 1 and words[0] == "Claude":
            words = words[1:]
        family = next((w for w in words if w and not w[0].isdigit()), None)
        version = next((w for w in words if w and w[0].isdigit()), None)
        if family is None:
            return (version or window[:3])[:3]
        if version:
            return family[:2] + "".join(p for p in version.split(".") if p)
        return family[:3]
    return window[:3]


def short_label(window: str) -> str:
    base, tag = split_account(window)
    text = _base_short_label(base)
    return text if tag == "default" else f"{text} {tag}"


# --- other providers: windows stored as "<provider>/<window>" ---
PROVIDER_SEP = "/"

# Display names in display order; Claude's bare window keys come first.
PROVIDERS = {
    "antigravity": "Antigravity",
    "opencode-go": "OpenCode Go",
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
}
PROVIDER_SHORT = {"antigravity": "ag", "opencode-go": "go", "ollama": "ol", "openrouter": "or"}

# Non-default accounts: "<provider>@<tag>/<window>"; Claude's bare keys become "claude@<tag>/<key>".
ACCOUNT_SEP = "@"
# Agents whose auth file holds several providers tag their windows "<agent>.<account>".
MULTI_PROVIDER_AGENTS = ("opencode", "prime-agent")
AGENT_PROVIDERS = {
    "claude": ("claude",),
    "antigravity": ("antigravity",),
    "ollama": ("ollama",),
    "opencode": ("opencode-go", "openrouter"),
    "prime-agent": ("opencode-go", "openrouter"),
}


def account_tag(agent: str, account: str) -> str:
    if account == "default":
        return "default"
    return f"{agent}.{account}" if agent in MULTI_PROVIDER_AGENTS else account


def split_account(window: str):
    head, sep, rest = window.partition(PROVIDER_SEP)
    provider, at, tag = head.partition(ACCOUNT_SEP)
    if not (sep and at and tag):
        return window, "default"
    return (rest if provider == "claude" else provider + PROVIDER_SEP + rest), tag


def with_account(window: str, tag: str) -> str:
    if not tag or tag == "default":
        return window
    head, sep, rest = window.partition(PROVIDER_SEP)
    if sep and head in PROVIDERS:
        return f"{head}{ACCOUNT_SEP}{tag}{PROVIDER_SEP}{rest}"
    return f"claude{ACCOUNT_SEP}{tag}{PROVIDER_SEP}{window}"

# Comma list limiting which providers the timer polls ("" = all with credentials).
ENABLED_PROVIDERS = tuple(p for p in os.environ.get("ALIMIT_PROVIDERS", "").split(",") if p)

# Credentials other agents already keep; alimit only reads them.
PRIME_AGENT_AUTH = _env_path("ALIMIT_PRIME_AGENT_AUTH", HOME / ".config" / "prime-agent" / "auth.json")
OPENCODE_AUTH = _env_path("OPENCODE_DATA_DIR", _DATA_HOME / "opencode") / "auth.json"

OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
OLLAMA_BALANCE_URL = "https://ollama.com/api/balance"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

# Window part of a provider key: (label, short label, rank within the provider).
PROVIDER_WINDOWS = {
    "5h": ("5-hour", "5h", 0),
    "rolling": ("5-hour", "5h", 0),
    "session": ("session", "ses", 0),
    "day": ("daily", "day", 1),
    "free_daily": ("free requests today", "free", 1),
    "seven_day": ("weekly", "wk", 2),
    "weekly": ("weekly", "wk", 2),
    "monthly": ("monthly", "mo", 3),
    "allowance": ("monthly allowance", "mo", 3),
    "credits": ("credit limit", "cr", 4),
}
# Antigravity quota keys are "<family>-<window>", e.g. "gemini-5h".
FAMILY_LABELS = {"gemini": ("Gemini", "gem"), "3p": ("third-party", "3p")}


def provider_of(window: str) -> str:
    head, sep, _ = split_account(window)[0].partition(PROVIDER_SEP)
    return head if sep and head in PROVIDERS else "claude"


def _provider_parts(window: str):
    """(provider, family label pair or None, window part) for a provider key."""
    provider, _, rest = window.partition(PROVIDER_SEP)
    family, _, part = rest.rpartition("-")
    if not family or part not in PROVIDER_WINDOWS:
        return provider, None, rest
    return provider, FAMILY_LABELS.get(family, (family, family[:3])), part


def provider_window_rank(window: str) -> int:
    _, _, part = _provider_parts(split_account(window)[0])
    return PROVIDER_WINDOWS.get(part, ("", "", 9))[2]


def is_weekly(window: str) -> bool:
    base = split_account(window)[0]
    if provider_of(window) == "claude":
        return base == "seven_day" or base.startswith(WEEKLY_SCOPED_PREFIX)
    return _provider_parts(base)[2] in ("weekly", "seven_day")


def period_of(window: str) -> str | None:
    """"week" or "month" for windows rates.paced projects; None keeps the last hour's rate."""
    if is_weekly(window):
        return "week"
    if provider_of(window) != "claude" and _provider_parts(split_account(window)[0])[2] in ("monthly", "allowance"):
        return "month"
    return None


# Monthly windows reach back far enough to find last month's total.
MONTH_HISTORY_MS = 40 * 86_400_000


def weekly_of(window: str) -> list[str]:
    """Keys of the weekly window paired with a 5-hour window (same provider, family, account)."""
    base, tag = split_account(window)
    if provider_of(window) == "claude":
        return [with_account("seven_day", tag)] if base == "five_hour" else []
    provider, _, rest = base.partition(PROVIDER_SEP)
    family, dash, part = rest.rpartition("-")
    if part not in ("5h", "rolling"):
        return []
    return [with_account(f"{provider}{PROVIDER_SEP}{family}{dash}{w}", tag) for w in ("weekly", "seven_day")]
