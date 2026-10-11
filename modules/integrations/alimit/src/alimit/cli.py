"""Command-line interface: watch (default), status, statusline, poll, contrib."""
import argparse
import dataclasses
import json as _json
import math
import os
import re
import shutil
import subprocess
import sys
import time

from . import __version__, accounts, config, poller, sources, speed, store
from .rates import compute, to_ms


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- formatting helpers ----------
def _color_on() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str, on: bool | None = None) -> str:
    if on is None:
        on = _color_on()
    return f"\033[{code}m{s}\033[0m" if on else s


def _util_code(u: float) -> str:
    return "32" if u < 50 else ("33" if u < 80 else "31")


def fmt_dur(ms) -> str:
    if ms is None:
        return "—"
    s = max(0, int(ms // 1000))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def runway_ms(r) -> float | None:
    """Countdown to the cap in ms, or None when there is none worth printing.

    A window already at 100% has a runway of zero, and "ends 0s" says nothing.
    """
    if not r.will_exhaust_before_reset or not r.runway_min:
        return None
    ms = r.runway_min * 60_000
    return ms if ms >= 1000 else None


def bar(util: float, width: int = 16) -> str:
    filled = max(0, min(width, round(util / 100 * width)))
    return "█" * filled + "░" * (width - filled)


# Column labels of the metric strip under each window's bar. The reset countdown
# sits on the group heading instead, see group_by_reset().
METRIC_LABELS = ("%/min", "%/hr", "%/8h", "%/day", "ends in")

# resets_at jitters between polls, so resets this close count as the same one.
RESET_GROUP_MS = 60_000


def _col_width() -> int:
    """Width of one metric column; the bar spans all of them.

    get_terminal_size already falls back to the size we pass when there is no
    terminal, so no error handling of our own is needed.
    """
    cols = shutil.get_terminal_size((96, 24)).columns
    return max(10, min(cols, 108) // len(METRIC_LABELS))


def collect(con, now_ms: int, lookback: int = 60, providers=None, tags=None):
    """The windows and rates every surface shows (terminal, widget, status line, alerts).

    Claude's default windows come first, then Claude accounts, then each other provider's;
    `providers` limits the list and `tags` keeps one account per provider."""
    def keep(w):
        p = config.provider_of(w)
        if providers and p not in providers:
            return False
        return tags is None or config.split_account(w)[1] == tags.get(p, "default")

    present = [w for w in store.windows_present(con) if config.is_known_window(w) and keep(w)]
    order = [w for w in config.WINDOW_ORDER if w in present]
    scoped = sorted(
        (w for w in present if w.startswith(config.WEEKLY_SCOPED_PREFIX)),
        key=lambda w: config.window_display_name(w),
    )

    def claude_rank(w):
        base = config.split_account(w)[0]
        idx = config.WINDOW_ORDER.index(base) if base in config.WINDOW_ORDER else len(config.WINDOW_ORDER)
        return (config.split_account(w)[1], idx, config.window_display_name(base))

    claude_accounts = sorted(
        (w for w in present if config.provider_of(w) == "claude" and config.split_account(w)[1] != "default"),
        key=claude_rank,
    )
    others = sorted(
        (w for w in present if config.provider_of(w) != "claude"),
        key=lambda w: (
            list(config.PROVIDERS).index(config.provider_of(w)),
            config.split_account(w)[1] != "default",
            config.split_account(w)[1],
            config.provider_window_rank(w),
            w,
        ),
    )
    present_order = order + scoped + claude_accounts + others
    out = []
    for w in store.prune_retired(con, present_order, now_ms):
        rows = store.samples_for(con, w, since_ts=now_ms - config.HISTORY_MS)
        r = compute(w, rows, now_ms, lookback_min=lookback)
        if r:
            out.append(r)
    return out


def shown(rlist):
    """Windows the views print: 0% ones are hidden. Alerts still get the full list."""
    return [r for r in rlist if round(r.util)]


def group_by_reset(rlist):
    """Runs of adjacent windows sharing a reset, as [(reset_ts, [Rate, ...]), ...]."""
    groups = []
    for r in rlist:
        last = groups[-1][0] if groups else None
        if r.reset_ts and last and abs(r.reset_ts - last) <= RESET_GROUP_MS:
            groups[-1][1].append(r)
        else:
            groups.append((r.reset_ts, [r]))
    return groups


def _reset_word(rates) -> str:
    """Verb agrees with the group: "5h resets in", "wk · Fab reset in"."""
    return "resets in" if len(rates) == 1 else "reset in"


# ---------- renderers ----------
def _metric_values(r, now_ms: int):
    # Runway past the reset never happens, so only a projected cap is shown.
    ms = runway_ms(r)
    runway = "⚠ " + fmt_dur(ms) if ms is not None else "—"
    return (
        f"{r.per_min:.2f}",
        f"{r.per_hour:.1f}",
        f"{r.per_8h:.1f}",
        f"{r.per_day:.1f}",
        runway,
    )


def _mark(r, marks) -> str:
    key = accounts.agent_account(r.window)
    return (marks or {}).get(key, "") if key else ""


def render_section(r, now_ms: int, col: int, mark: str = "") -> str:
    """One window as a block: heading + used%, full-width bar, metric strip.

    Mirrors the Plasma widget's popup (plasmoid/contents/ui/main.qml).
    """
    code = _util_code(r.util)
    width = col * len(METRIC_LABELS)

    name = config.label(r.window) + (f" {mark}" if mark else "") + (" ·stale" if r.stale else "")
    used = f"{r.util:.0f}%"
    warn = "⚠ hits cap before reset" if r.will_exhaust_before_reset else ""
    # pad on the plain text, colour afterwards, so the escapes never shift columns
    right = f"{warn}  {used}" if warn else used
    gap = " " * max(1, width - len(name) - len(right))
    head = _c("1", name) + gap + (_c("31;1", warn) + "  " if warn else "") + _c(f"{code};1", used)

    labels = "".join(label.center(col) for label in METRIC_LABELS)
    cells = [v.center(col) for v in _metric_values(r, now_ms)]
    if runway_ms(r) is not None:
        cells[4] = _c("31", cells[4])

    # blank line under the bar, mirroring the widget's spacing
    return "\n".join([head, _c(code, bar(r.util, width)), "", _c("2", labels), "".join(cells)])


def render_table(rlist, now_ms: int, marks=None) -> str:
    rlist = shown(rlist)
    if not rlist:
        return "no usage yet. Run Claude Code, or check `systemctl --user status alimit.timer`."
    col = _col_width()
    width = col * len(METRIC_LABELS)
    blocks = []
    for reset_ts, rates in group_by_reset(rlist):
        title = f"{_reset_word(rates)} {fmt_dur(reset_ts - now_ms)} " if reset_ts else "no reset "
        blocks.append(_c("2", title + "─" * max(0, width - len(title))))
        blocks.append("\n\n".join(render_section(r, now_ms, col, _mark(r, marks)) for r in rates))
    return "\n\n".join(blocks)


# Nerd Font glyphs; all have ink wider than one cell so kitty (non-Mono font) spaces them alike.
# nf-md-restore, nf-md-clock_alert_outline: time to window reset, projected time to cap.
RESET_GLYPH, ENDS_GLYPH = "\U000F099B ", "\U000F05CE "
# nf-md-folder, nf-md-git
DIR_GLYPH, BRANCH_GLYPH = "\U000F024B ", "\U000F02A2 "
# (glyph, colour): nf-md-network_strength_off / _1.._4, nf-md-lightning_bolt_circle; cool to hot as effort rises
EFFORT_GLYPHS = {"off": ("\U000F08FC", "2"), "low": ("\U000F08F4", "32"), "medium": ("\U000F08F6", "33"),
                 "high": ("\U000F08F8", "38;5;208"), "xhigh": ("\U000F08FA", "31"), "max": ("\U000F0820", "31;1")}
SEP, RULE = " \u2502 ", "\u2500"
# rule junction by (separator above, separator below)
JOINTS = {(True, True): "\u253c", (True, False): "\u2534", (False, True): "\u252c", (False, False): RULE}


def pie(pct: float) -> str:
    """nf-md-circle_slice_1..8 filled in 12.5% steps."""
    return chr(0xF0A9E + min(7, max(0, math.ceil(pct / 12.5) - 1)))


def render_statusline(rlist, now_ms: int, color: bool | None = None) -> str:
    """Compact one-line summary, e.g. `5h 31% 2.1/h 󰔟 1h17m 󰦛 1h25m | wk 74% · opus 12% 󰦛 2d4h`."""
    rlist = shown(rlist)
    if not rlist:
        return "alimit: no usage"
    groups = []
    for reset_ts, rates in group_by_reset(rlist):
        groups.append(_c("2", " · ", color).join(_statusline_segment(r, color) for r in rates))
        if reset_ts:
            groups[-1] += " " + _c("2", RESET_GLYPH + fmt_dur(reset_ts - now_ms), color)
    return _c("2", SEP, color).join(groups)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _width(s: str) -> int:
    return len(_ANSI.sub("", s))


def _table(rows, color: bool | None = None) -> str:
    """Bordered table with a rule between rows; a cell is a str, or (str, span) covering `span` columns.

    Columns widen so a spanning cell fits, extra width shared evenly."""
    rows = [[c if isinstance(c, tuple) else (c, 1) for c in row] for row in rows]
    ncols = max(sum(span for _, span in row) for row in rows)
    rows = [row + [("", ncols - n)] if (n := sum(span for _, span in row)) < ncols else row for row in rows]
    widths = [0] * ncols
    for row in rows:
        col = 0
        for text, span in row:
            if span == 1:
                widths[col] = max(widths[col], _width(text))
            col += span
    for row in rows:
        col = 0
        for text, span in row:
            need = _width(text) - (sum(widths[col:col + span]) + 3 * (span - 1))
            for k in range(span):
                widths[col + k] += max(0, need // span + (k < need % span))
            col += span

    def rule(left, right, above, below):
        segs = [RULE * (w + 2) for w in widths]
        return _c("2", left + "".join(seg + JOINTS[(k in above, k in below)]
                                       for k, seg in enumerate(segs, 1))[:-1] + right, color)

    lines, prev = [], set()
    for row in rows:
        col, cells, joints = 0, [], set()
        for text, span in row:
            width = sum(widths[col:col + span]) + 3 * (span - 1)
            cells.append(" " + text + " " * (width - _width(text)) + " ")
            col += span
            joints.add(col)
        joints.discard(ncols)
        lines.append(rule("\u250c", "\u2510", prev, joints) if not lines else rule("\u251c", "\u2524", prev, joints))
        bar = _c("2", "\u2502", color)
        lines.append(bar + bar.join(cells) + bar)
        prev = joints
    lines.append(rule("\u2514", "\u2518", prev, set()))
    return "\n".join(lines)


def _bar_label(window: str, single: bool) -> str:
    """short_label, minus the provider prefix on a bar showing one provider only."""
    label = config.short_label(window)
    if single and config.provider_of(window) != "claude":
        return label.partition(" ")[2] or label
    return label


def _statusline_segment(r, color, label_width: int = 0, single: bool = False) -> str:
    # a red runway already marks a window that caps before reset, so no separate alert
    seg = [_bar_label(r.window, single).rjust(label_width), _c(f"{_util_code(r.util)};1", f"{r.util:.0f}%", color)]
    if r.per_hour >= 0.05:
        seg.append(_c("2", f"{r.per_hour:.1f}/h", color))
    if (ms := runway_ms(r)) is not None:
        seg.append(_c("31", ENDS_GLYPH + fmt_dur(ms), color))
    return " ".join(seg)


# p10k theme colours (grey context/vcs, purple dir, cyan ahead/behind), plus Claude orange for the model.
GREY, PURPLE, CYAN, ORANGE = "38;5;242", "38;2;145;65;172", "38;2;33;144;164", "38;2;217;119;87"


def _git(cwd: str):
    """(branch, dirty, behind, ahead) for cwd, or None outside a work tree."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", cwd, "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    oid, branch, dirty, ahead, behind = "", "", False, 0, 0
    for line in proc.stdout.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.split()[2]
        elif line.startswith("# branch.head "):
            branch = line.split(maxsplit=2)[2]
        elif line.startswith("# branch.ab "):
            a, b = line.split()[2:4]
            ahead, behind = int(a), -int(b)
        elif not line.startswith("#"):
            dirty = True
    if branch == "(detached)":
        branch = "@" + oid[:8]
    return branch, dirty, behind, ahead


def _auto_compact_window() -> int | None:
    try:
        v = _json.loads(config.SETTINGS_PATH.read_text()).get("autoCompactWindow")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and v > 0 else None


def context_pct(payload, compact_window: int | None = None) -> float | None:
    """Context window fill from the status line payload, or None before the first response.

    Measured against min(model window, autoCompactWindow) so 100% means compaction."""
    ctx = payload.get("context_window") or {}
    usage, size = ctx.get("current_usage") or {}, ctx.get("context_window_size")
    pct = ctx.get("used_percentage")
    if usage:
        used = sum(usage.get(k) or 0 for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    elif isinstance(pct, (int, float)) and size:
        used = pct * size / 100
    elif isinstance(pct, (int, float)):
        return float(pct)
    else:
        return None
    limits = [w for w in (size, compact_window) if w]
    return 100.0 * used / min(limits) if limits else None


def model_label(payload) -> str | None:
    """Thinking level glyph from EFFORT_GLYPHS, then the model name, e.g. "󰣺 Opus 5.5"."""
    model = payload.get("model") or {}
    name = model.get("display_name") or model.get("id")
    if not name:
        return None
    if (payload.get("thinking") or {}).get("enabled") is False:
        level = "off"
    else:
        level = (payload.get("effort") or {}).get("level")
    if not level and isinstance(model.get("effort"), str):
        # Antigravity: model.effort, and the display name repeats it ("Gemini 3.8 Flash (High)")
        level = model["effort"].lower()
        name = re.sub(r"\s*\(" + re.escape(level) + r"\)$", "", name, flags=re.IGNORECASE)
    name = _c(ORANGE, name, True)
    if level in EFFORT_GLYPHS:
        glyph, code = EFFORT_GLYPHS[level]
        return _c(code, glyph, True) + " " + name
    # an unknown level name stays as text after the name
    return name + (" " + _c(GREY, level, True) if level else "")


def _short_path(cwd: str, project: str | None) -> str:
    """cwd relative to the session's project dir (named by its basename), else ~-shortened.

    Deep paths keep the first and last two parts: `proj/…/alimit/src`."""
    project = (project or "").rstrip("/")
    home = str(config.HOME)
    if project and (cwd == project or cwd.startswith(project + "/")):
        path = os.path.basename(project) + cwd[len(project):]
    elif cwd == home or cwd.startswith(home + "/"):
        path = "~" + cwd[len(home):]
    else:
        path = cwd
    parts = path.split("/")
    return path if len(parts) <= 3 else "/".join([parts[0], "\u2026", *parts[-2:]])


def render_bar(rlist, now_ms: int) -> str:
    """Bordered table with one cell per usage window, then the resets, one per group sharing one."""
    active = shown(rlist)
    single = len({config.provider_of(r.window) for r in active}) == 1
    label_width = max((len(_bar_label(r.window, single)) for r in active), default=0)
    usage, resets = [], []
    for reset_ts, rates in group_by_reset(active):
        usage += [_statusline_segment(r, True, label_width, single) for r in rates]
        resets.append((_c("2", RESET_GLYPH + fmt_dur(reset_ts - now_ms), True) if reset_ts else "", len(rates)))
    rows = [usage or ["alimit: no usage"]]
    if any(text for text, _ in resets):
        rows.append(resets)
    return _table(rows, color=True)


def render_prompt(payload, rlist, now_ms: int, agent: str = "claude") -> str:
    """Agent status line: a title (dir, git, model, context, tok/s) over render_bar's table."""
    payload = payload if isinstance(payload, dict) else {}
    workspace = payload.get("workspace") or {}
    cwd = workspace.get("current_dir") or payload.get("cwd") or os.getcwd()
    where = _c(PURPLE, DIR_GLYPH + _short_path(cwd, workspace.get("project_dir")), True)
    git = _git(cwd)
    if git is None and (vcs := payload.get("vcs") or {}).get("branch"):
        git = (vcs["branch"], bool(vcs.get("dirty")), 0, 0)
    if git:
        branch, dirty, behind, ahead = git
        arrows = ("⇣" if behind else "") + ("⇡" if ahead else "")
        where += " " + _c(GREY, BRANCH_GLYPH + branch, True) + (_c("33", "*", True) if dirty else "") \
            + (_c(CYAN, arrows, True) if arrows else "")
    head = [where]
    if model := model_label(payload):
        head.append(model)
    # autoCompactWindow is a Claude Code setting
    compact = _auto_compact_window() if agent == "claude" else None
    if (ctx := context_pct(payload, compact)) is not None:
        head.append(_c(_util_code(ctx), f"{pie(ctx)} {ctx:.0f}%", True))
    if rate := speed.render(payload.get("transcript_path")):
        last, sep, avg = rate.partition(" · ")
        head.append(_c(CYAN, last, True) + (_c("2", sep, True) + _c(GREY, avg, True) if avg else ""))
    return "  ".join(head) + "\n" + render_bar(rlist, now_ms)


def cross_metric(rlist):
    """Exchange rate between the 5h session and weekly windows, or None when idle.

    Both windows rise from the same usage, so (5h burn / weekly burn) is a fairly
    stable "how much of the session does 1% of weekly cost" number.
    """
    by = {r.window: r for r in rlist}
    fh, wk = by.get("five_hour"), by.get("seven_day")
    if not fh or not wk or wk.per_hour <= 1e-6:
        return None
    ratio = fh.per_hour / wk.per_hour
    headroom = (100.0 - fh.util) / ratio if ratio > 1e-9 else None
    return {
        "session_pct_per_weekly_pct": ratio,
        "weekly_pct_until_session_caps": headroom,
    }


def render_cross(rlist) -> str | None:
    c = cross_metric(rlist)
    if not c:
        return None
    line = f"1% weekly usage ≈ {c['session_pct_per_weekly_pct']:.1f}% of 5-hour usage"
    if c["weekly_pct_until_session_caps"] is not None:
        line += f"  ·  ~{c['weekly_pct_until_session_caps']:.0f}% more weekly usage before 5-hour usage caps"
    return _c("2", line)


def render_json(rlist, now_ms: int, marks=None) -> str:
    cross = cross_metric(rlist)
    rlist = shown(rlist)
    group_of = {r.window: i for i, (_, rates) in enumerate(group_by_reset(rlist)) for r in rates}
    single = len({config.provider_of(r.window) for r in rlist}) == 1
    return _json.dumps(
        {
            "now_ms": now_ms,
            "windows": [
                {
                    **dataclasses.asdict(r),
                    "label": config.label(r.window),
                    "short_label": config.short_label(r.window),
                    "bar_label": _bar_label(r.window, single),
                    "reset_group": group_of[r.window],
                    "account": config.split_account(r.window)[1],
                    "mark": _mark(r, marks),
                }
                for r in rlist
            ],
            "cross": cross,
        },
        indent=2,
    )


# ---------- commands ----------
def cmd_status(args) -> int:
    con = store.connect()
    now = _now_ms()
    rlist = collect(con, now, lookback=args.lookback, providers=_providers(args))
    marks = accounts.marks(con, now)
    if args.json:
        print(render_json(rlist, now, marks=marks))
    elif args.statusline:
        print(render_statusline(rlist, now))
    else:
        print(render_table(rlist, now, marks=marks))
        cross = render_cross(rlist)
        if cross:
            print()
            print(cross)
    return 0


def cmd_watch(args) -> int:
    con = store.connect()
    refresh = max(2, args.refresh)
    try:
        while True:
            now = _now_ms()
            rlist = collect(con, now, lookback=args.lookback)
            marks = accounts.marks(con, now)
            sys.stdout.write("\033[2J\033[H")
            print(_c("2", f"{time.strftime('%H:%M:%S')}   "
                          f"(refresh {refresh}s · Ctrl-C to quit)\n"))
            print(render_table(rlist, now, marks=marks))
            cross = render_cross(rlist)
            if cross:
                print()
                print(cross)
            time.sleep(refresh)
    except KeyboardInterrupt:
        print()
        return 0


def _read_payload():
    if sys.stdin.isatty():
        return {}
    try:
        payload = _json.load(sys.stdin)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _record_payload(con, agent: str, payload, now: int) -> None:
    """Store the usage windows an agent's status line payload carries."""
    from . import providers

    if agent == "claude":
        windows = sources.parse_statusline(payload)
    else:
        windows = providers.PAYLOAD_PARSERS[agent](payload, now)
    # An idle session keeps its last windows; once one has reset it is history.
    windows = {k: w for k, w in windows.items() if (to_ms(w["resets_at"]) or now + 1) > now}
    tags = accounts.env_tags() or {}
    windows = {config.with_account(k, tags.get(config.provider_of(k), "default")): w for k, w in windows.items()}
    if windows:
        store.record(con, now, windows, "statusline")


# Agents whose status line payload alimit reads; each bar shows its own provider by default.
PAYLOAD_AGENTS = ("claude", "antigravity")


def _providers(args, default=None):
    """--provider values ("all" = every provider), else the default set."""
    chosen = [p for v in (args.provider or []) for p in v.split(",") if p]
    if "all" in chosen:
        return None
    return set(chosen) if chosen else default


def cmd_statusline(args) -> int:
    payload = _read_payload()
    now = _now_ms()
    con = store.connect()
    _record_payload(con, args.agent, payload, now)
    rlist = collect(con, now, providers=_providers(args, {args.agent}), tags=accounts.env_tags())
    print(render_prompt(payload, rlist, now, agent=args.agent))
    return 0


def cmd_bar(args) -> int:
    now = _now_ms()
    print(render_bar(collect(store.connect(), now, providers=_providers(args), tags=accounts.env_tags()), now))
    return 0


def cmd_record(args) -> int:
    now = _now_ms()
    _record_payload(store.connect(), args.agent, _read_payload(), now)
    return 0


def cmd_poll(args) -> int:
    con = store.connect()
    results = poller.poll_all(con)
    for name, (status, n) in results.items():
        print(f"{name}: {status} (+{n} rows)")
    if not args.no_alerts:
        from . import notify

        now = _now_ms()
        style = notify.AlertStyle(
            urgency=args.alert_urgency,
            timeout=args.alert_timeout,
            transient=args.alert_transient,
        )
        notify.check(con, collect(con, now), now, style)
    return 1 if any(s.startswith("error") for s, _ in results.values()) else 0


def cmd_contrib(args) -> int:
    from . import contrib

    root = accounts.state_dir() / "claude" / args.account / "projects" if args.account else None
    acc = contrib.analyze(hours=args.hours, root=root)
    print(contrib.to_json(acc, args.hours) if args.json else contrib.render(acc, args.hours))
    return 0


def cmd_headroom(args) -> int:
    con = store.connect()
    now = _now_ms()
    names = [n for n in args.eligible.split(",") if n]
    rows = accounts.headroom(con, args.agent, names, now)
    print(_json.dumps({
        "agent": args.agent,
        "pick": accounts.rank(rows, args.threshold, names),
        "accounts": [dataclasses.asdict(r) for r in rows],
    }))
    return 0


PROVIDER_HELP = "limit to a provider: claude, " + ", ".join(config.PROVIDERS) + " or all; repeatable"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alimit",
        description="Track AI usage limits + burn rate (Claude, Antigravity, OpenCode Go, Ollama, OpenRouter). "
                    "Bare invocation runs the live dashboard.",
    )
    p.add_argument("--version", action="version", version=f"alimit {__version__}")
    sub = p.add_subparsers(dest="cmd")

    st = sub.add_parser("status", help="print current usage + rates once and exit")
    st.add_argument("--lookback", type=int, default=60, help="rate window in minutes")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.add_argument("--statusline", action="store_true", help="one-line output for bars/tmux")
    st.add_argument("--provider", action="append", help=PROVIDER_HELP)

    sl = sub.add_parser(
        "statusline",
        help="agent status line: records the usage windows piped on stdin, prints the line",
    )
    sl.add_argument("--agent", choices=PAYLOAD_AGENTS, default="claude", help="whose payload is on stdin")
    sl.add_argument("--provider", action="append",
                    help=PROVIDER_HELP + " (default: the agent's own)")

    br = sub.add_parser("bar", help="just the usage table, for agents without a status line payload")
    br.add_argument("--provider", action="append", help=PROVIDER_HELP)

    rc = sub.add_parser("record", help="store the usage windows in a status line payload on stdin")
    rc.add_argument("agent", choices=PAYLOAD_AGENTS)

    pl = sub.add_parser("poll", help="fetch every provider's usage once (rate-limit safe), then alert")
    pl.add_argument("--no-alerts", action="store_true", help="disable notify-send alerts")
    pl.add_argument("--alert-urgency", choices=("low", "normal", "critical"), default="normal",
                    help="alert urgency; critical never auto-expires on Plasma")
    pl.add_argument("--alert-timeout", type=int, default=10,
                    help="seconds an alert stays on screen (0 = until dismissed)")
    pl.add_argument("--alert-transient", action="store_true",
                    help="don't keep alerts in the notification history")

    cb = sub.add_parser("contrib", help="what's contributing to your usage (local, approximate)")
    cb.add_argument("--hours", type=int, default=24, help="lookback window in hours (default 24)")
    cb.add_argument("--json", action="store_true", help="machine-readable output")
    cb.add_argument("--account", help="an ai-accounts Claude account (default: the real ~/.claude)")

    hr = sub.add_parser("headroom", help="per-account usage for ai-account's auto-pick, as JSON")
    hr.add_argument("--agent", required=True, choices=sorted(config.AGENT_PROVIDERS))
    hr.add_argument("--threshold", type=float, default=90)
    hr.add_argument("--eligible", default="default", help="comma list of logged-in accounts, in preference order")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:  # bare `alimit` → live watch dashboard
        args.cmd = "watch"
        args.lookback = 60
        args.refresh = 10
    return {
        "status": cmd_status,
        "watch": cmd_watch,
        "statusline": cmd_statusline,
        "bar": cmd_bar,
        "record": cmd_record,
        "poll": cmd_poll,
        "contrib": cmd_contrib,
        "headroom": cmd_headroom,
    }[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())