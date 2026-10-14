"""Reset-aware burn-rate math over the stored sample series.

Rate is a windowed average: (peak_now - peak_at(t_start)) / elapsed, where
t_start = max(now - lookback, start of the current window). Once the last rise
ages out of the lookback, t_start moves back to the sample before that rise, so
an idle window's rate decays as 1/t instead of dropping to zero. Usage inside a
window rarely goes down, so the running peak absorbs sources that round
differently (the endpoint can say 75 while the status line says 74).
"""
import calendar
from dataclasses import dataclass, replace
from datetime import datetime, timezone

# A window starts when resets_at moves later by more than this; sub-second jitter doesn't count.
RESET_SHIFT_MS = 10 * 60_000
# Without resets_at on both rows, a drop this far below the window's peak marks a reset.
RESET_DROP = 5.0
# Weekly rise needed before exchange() reports a ratio.
EXCHANGE_MIN_WEEKLY = 1.0
WEEK_MS = 7 * 86_400_000
# Share of the period paced() blends in the previous one's average: 48h of a week.
PRIOR_SHARE = 2 / 7


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None


def to_ms(resets_at):
    """ISO-8601 reset time to unix ms, or None."""
    dt = parse_iso(resets_at)
    return int(dt.timestamp() * 1000) if dt else None


def _is_poll(row):
    # rows without a source (tests, old callers) count as authoritative
    return len(row) < 4 or row[3] == "poll"


def _window_rows(rows):
    """Rows of the current window, minus late reports from the previous one.

    A poll well below the peak starts a new segment even with resets_at unchanged
    (weekly usage can be credited back); status-line rows above the last poll after
    that are idle sessions redrawing pre-drop numbers.
    """
    start, peak, reset = 0, rows[0][1], to_ms(rows[0][2])
    last_poll = rows[0][1] if _is_poll(rows[0]) else None
    dropped = False
    stale = set()
    for i in range(1, len(rows)):
        util, resets_at = rows[i][1], rows[i][2]
        poll = _is_poll(rows[i])
        r = to_ms(resets_at)
        if r is not None and reset is not None:
            if r < reset - RESET_SHIFT_MS:
                stale.add(i)  # a late report from the previous window (an idle session's redraw)
                continue
            new_window = r > reset + RESET_SHIFT_MS
        else:
            new_window = util < peak - RESET_DROP
        if new_window:
            dropped = False
        elif poll and util < peak - RESET_DROP:
            new_window = dropped = True
        elif not poll and dropped and last_poll is not None and util > last_poll + RESET_DROP:
            stale.add(i)
            continue
        if poll:
            last_poll = util
        if new_window:
            start, peak, reset = i, util, r
        else:
            peak = max(peak, util)
            reset = reset if reset is not None else r
    return [row for i, row in enumerate(rows) if i >= start and i not in stale]


@dataclass
class Rate:
    window: str
    util: float
    resets_at: str | None
    per_min: float
    per_hour: float
    per_8h: float
    per_day: float
    runway_min: float | None            # minutes to 100% at current rate; None = never
    exhaust_ts: int | None              # unix ms projected exhaustion
    reset_ts: int | None                # unix ms window reset
    will_exhaust_before_reset: bool
    stale: bool
    last_ts: int
    exchange: float | None = None       # weekly only: 5h percent spent per 1% of this window


def _projection(util, per_min, reset_ts, now_ms):
    """(runway_min, exhaust_ts, will_exhaust_before_reset) at a steady per_min."""
    if per_min <= 1e-6:
        return None, None, False
    runway_min = max(100.0 - util, 0.0) / per_min
    exhaust_ts = int(now_ms + runway_min * 60_000)
    return runway_min, exhaust_ts, bool(reset_ts is not None and exhaust_ts < reset_ts)


def compute(window, rows, now_ms, lookback_min=60, stale_after_min=30):
    """rows: (ts, util, resets_at) ascending. Returns Rate or None if no data."""
    if not rows:
        return None
    latest_ts = rows[-1][0]
    stale = (now_ms - latest_ts) > stale_after_min * 60_000
    win = _window_rows(rows)
    resets_at = next((row[2] for row in reversed(win) if row[2]), None)
    reset_ts = to_ms(resets_at)

    # The window reset with no sample since: usage is back to zero.
    if reset_ts is not None and now_ms >= reset_ts:
        return Rate(window, 0.0, None, 0.0, 0.0, 0.0, 0.0, None, None, None, False, stale, latest_ts)

    util_now = max(row[1] for row in win)
    # the sample before the window first hit its peak; usage is whole percents, so a rise is a step
    first_peak = next(i for i, row in enumerate(win) if row[1] == util_now)
    t_prev = win[first_peak - 1][0] if first_peak else win[0][0]
    t_start = max(min(now_ms - lookback_min * 60_000, t_prev), win[0][0])
    util_start = max((row[1] for row in win if row[0] <= t_start), default=win[0][1])
    span_min = max((now_ms - t_start) / 60_000, 1e-9)
    per_min = max((util_now - util_start) / span_min, 0.0)
    per_hour, per_8h, per_day = per_min * 60, per_min * 480, per_min * 1440

    runway_min, exhaust_ts, will = _projection(util_now, per_min, reset_ts, now_ms)
    return Rate(
        window=window,
        util=util_now,
        resets_at=resets_at,
        per_min=per_min,
        per_hour=per_hour,
        per_8h=per_8h,
        per_day=per_day,
        runway_min=runway_min,
        exhaust_ts=exhaust_ts,
        reset_ts=reset_ts,
        will_exhaust_before_reset=will,
        stale=stale,
        last_ts=latest_ts,
    )


def exchange(short_rows, weekly_rows):
    """5h percent spent per 1% of weekly over the stored history, or None.

    Only rises both windows report at the same instants count, and a reset of either one
    rebaselines both, so resets and gaps drop out of both sides. Polls are used when there are
    any; otherwise a status line row below the running peak is an idle session's redraw."""
    if any(_is_poll(row) for row in short_rows):
        short_rows = [row for row in short_rows if _is_poll(row)]
    weekly_at = {row[0]: row for row in weekly_rows}
    short_sum = weekly_sum = 0.0
    prev = None
    for s_row in short_rows:
        w_row = weekly_at.get(s_row[0])
        if w_row is None:
            continue
        cur = (s_row[1], to_ms(s_row[2]), w_row[1], to_ms(w_row[2]))
        if prev and not _is_poll(s_row) and _redraw(prev, cur):
            continue
        if prev and not _new_window(prev[0], prev[1], cur[0], cur[1]) \
                and not _new_window(prev[2], prev[3], cur[2], cur[3]):
            short_sum += max(0.0, cur[0] - prev[0])
            weekly_sum += max(0.0, cur[2] - prev[2])
            cur = (max(cur[0], prev[0]), cur[1], max(cur[2], prev[2]), cur[3])
        prev = cur
    if weekly_sum < EXCHANGE_MIN_WEEKLY or short_sum <= 0:
        return None
    return short_sum / weekly_sum


def _redraw(prev, cur):
    """A drop with both resets unchanged: old numbers, not a new window."""
    return all(a is not None and b is not None and abs(a - b) <= RESET_SHIFT_MS
               for a, b in ((prev[1], cur[1]), (prev[3], cur[3]))) \
        and (cur[0] < prev[0] - RESET_DROP or cur[2] < prev[2] - RESET_DROP)


def _new_window(util, reset, util2, reset2):
    # an idle 5h window reports no resets_at, so a missing one on either side counts as a reset
    if reset is None or reset2 is None:
        return reset is not reset2 or util2 < util - RESET_DROP
    return abs(reset - reset2) > RESET_SHIFT_MS or util2 < util - RESET_DROP


def _month_before(ms):
    """The same day and time one calendar month earlier, clamped to that month's length."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    year, month = (dt.year, dt.month - 1) if dt.month > 1 else (dt.year - 1, 12)
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return int(dt.replace(year=year, month=month, day=day).timestamp() * 1000)


def paced(r, rows, now_ms, period="week"):
    """The Rate burning at its average since the window opened, not the last hour's.

    Backtested on a week of samples, this was the closest to where weekly usage ended up.
    Early on it leans on the previous period's average, so one early burst isn't a week."""
    if r.reset_ts is None:
        return r
    start = r.reset_ts - WEEK_MS if period == "week" else _month_before(r.reset_ts)
    length = r.reset_ts - start
    elapsed = max(now_ms - start, 3_600_000)
    per_min = r.util / (elapsed / 60_000)
    # the previous window reset by this one's start; Antigravity's opens later, at first use
    prev = [row[1] for row in rows
            if (t := to_ms(row[2])) is not None and start - length < t <= start + RESET_SHIFT_MS]
    if prev and elapsed < length * PRIOR_SHARE:
        w = elapsed / (length * PRIOR_SHARE)
        per_min = w * per_min + (1 - w) * max(prev) / (length / 60_000)
    runway_min, exhaust_ts, will = _projection(r.util, per_min, r.reset_ts, now_ms)
    return replace(
        r, per_min=per_min, per_hour=per_min * 60, per_8h=per_min * 480, per_day=per_min * 1440,
        runway_min=runway_min, exhaust_ts=exhaust_ts, will_exhaust_before_reset=will,
    )
