import unittest
from datetime import datetime, timezone

from alimit.rates import WEEK_MS, compute, exchange, paced, parse_iso

MIN = 60_000
FUTURE = "2999-01-01T00:00:00+00:00"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def compute_ok(*args, **kwargs):
    # compute() is Optional; every case here feeds data, so narrow once
    r = compute(*args, **kwargs)
    assert r is not None
    return r


class TestRates(unittest.TestCase):
    def test_flat_no_burn(self):
        rows = [(0, 20.0, FUTURE), (50 * MIN, 20.0, FUTURE)]
        r = compute_ok("w", rows, 100 * MIN, lookback_min=60)
        self.assertEqual(r.util, 20.0)
        self.assertAlmostEqual(r.per_min, 0.0, places=6)
        self.assertIsNone(r.runway_min)
        self.assertFalse(r.will_exhaust_before_reset)

    def test_linear_burn_per_hour(self):
        rows = [(0, 10.0, FUTURE), (60 * MIN, 20.0, FUTURE)]  # +10 over 60 min
        r = compute_ok("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 10.0, places=3)
        self.assertAlmostEqual(r.per_8h, 80.0, places=3)
        self.assertAlmostEqual(r.per_day, 240.0, places=2)
        assert r.runway_min is not None  # narrows for the type checker
        self.assertAlmostEqual(r.runway_min, 480.0, places=1)  # 80 left / 10 per hr = 8h

    def test_lookback_carry_forward(self):
        # burst to 30 at t=20min, then flat; 60-min window anchors at util_start=10
        rows = [(0, 10.0, FUTURE), (20 * MIN, 30.0, FUTURE), (60 * MIN, 30.0, FUTURE)]
        r = compute_ok("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 20.0, places=3)

    def test_idle_window_rate_decays_instead_of_dropping(self):
        # one 1% step at t=10min, nothing since: 1%/h while inside the lookback, then 1% over the elapsed time
        rows = [(0, 50.0, None), (10 * MIN, 51.0, None)]
        self.assertAlmostEqual(compute_ok("w", rows, 60 * MIN, lookback_min=60).per_hour, 1.0)
        self.assertAlmostEqual(compute_ok("w", rows, 120 * MIN, lookback_min=60).per_hour, 0.5)
        self.assertAlmostEqual(compute_ok("w", rows, 240 * MIN, lookback_min=60).per_hour, 0.25)

    def test_reset_never_negative(self):
        # util drops 90 -> 5 with a resets_at change: boundary moves, rate stays >= 0
        rows = [(0, 80.0, "A"), (60 * MIN, 90.0, "A"), (120 * MIN, 5.0, "B"), (130 * MIN, 7.0, "B")]
        r = compute_ok("w", rows, 130 * MIN, lookback_min=60)
        self.assertGreaterEqual(r.per_min, 0.0)
        self.assertAlmostEqual(r.per_hour, 12.0, places=1)  # +2 over 10 min post-reset

    def test_exhaust_vs_reset_flag(self):
        base = 1_800_000_000_000  # arbitrary realistic epoch ms
        reset = iso(base + 30 * MIN)  # window resets in 30 min
        # 50%/hr → 50 left → 60 min runway > 30 min → resets first
        slow = [(base - 60 * MIN, 0.0, reset), (base, 50.0, reset)]
        self.assertFalse(compute_ok("w", slow, base, lookback_min=60).will_exhaust_before_reset)
        # 80%/hr → 20 left → 15 min runway < 30 min → hits cap first
        fast = [(base - 60 * MIN, 0.0, reset), (base, 80.0, reset)]
        self.assertTrue(compute_ok("w", fast, base, lookback_min=60).will_exhaust_before_reset)

    def test_parse_iso(self):
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00.015050+00:00"))
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00Z"))
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso("not-a-date"))

    def test_jitter_resets_at_not_a_reset(self):
        # Sub-second resets_at wobble must NOT trigger a window reset — burn would collapse to 0.
        rows = [
            (0, 27.0, "2026-07-21T21:00:00.1+00:00"),
            (30 * MIN, 28.0, "2026-07-21T20:59:59.9+00:00"),
            (60 * MIN, 29.0, "2026-07-21T21:00:00.2+00:00"),
        ]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 2.0, places=3)  # +2 over 60 min, not 0
        self.assertIsNotNone(r.runway_min)

    def test_sources_rounding_differently_is_not_a_reset(self):
        # the endpoint says 75, the status line says 74: the peak holds, the rate stays positive
        reset = "2999-01-01T21:00:00+00:00"
        rows = [(0, 70.0, reset), (30 * MIN, 75.0, reset), (40 * MIN, 74.0, reset), (60 * MIN, 75.0, reset)]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertEqual(r.util, 75.0)
        self.assertAlmostEqual(r.per_hour, 5.0, places=3)

    def test_reset_detected_by_resets_at_moving(self):
        # a small window (3% -> 1%) resets without a big drop; resets_at moving marks it
        rows = [(0, 3.0, iso(100 * MIN)), (200 * MIN, 1.0, iso(500 * MIN)), (230 * MIN, 2.0, iso(500 * MIN))]
        r = compute_ok("five_hour", rows, 230 * MIN, lookback_min=60)
        self.assertEqual(r.util, 2.0)
        self.assertAlmostEqual(r.per_hour, 2.0, places=3)  # +1 over the 30 min since the reset

    def test_late_report_from_previous_window_is_ignored(self):
        rows = [(0, 40.0, iso(100 * MIN)), (110 * MIN, 1.0, iso(400 * MIN)), (120 * MIN, 40.0, iso(100 * MIN))]
        r = compute_ok("five_hour", rows, 120 * MIN, lookback_min=60)
        self.assertEqual(r.util, 1.0)

    def test_poll_drop_with_same_resets_at_starts_new_segment(self):
        # weekly usage credited back (63 -> 19) while resets_at holds; idle sessions keep redrawing 63
        reset = "2999-01-01T21:00:00+00:00"
        rows = [
            (0, 63.0, reset, "poll"),
            (10 * MIN, 63.0, reset, "statusline"),
            (20 * MIN, 19.0, reset, "poll"),
            (30 * MIN, 63.0, reset, "statusline"),
            (40 * MIN, 21.0, reset, "statusline"),
            (60 * MIN, 22.0, reset, "poll"),
        ]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertEqual(r.util, 22.0)
        self.assertAlmostEqual(r.per_hour, 4.5, places=3)  # +3 over the 40 min since the drop

    def test_statusline_drop_alone_is_not_a_reset(self):
        # an idle session's old, lower numbers must not reset the peak
        reset = "2999-01-01T21:00:00+00:00"
        rows = [(0, 60.0, reset, "poll"), (30 * MIN, 18.0, reset, "statusline")]
        self.assertEqual(compute_ok("seven_day", rows, 30 * MIN).util, 60.0)

    def test_passed_reset_reads_zero(self):
        rows = [(0, 60.0, iso(100 * MIN))]
        r = compute_ok("five_hour", rows, 101 * MIN, lookback_min=60)
        self.assertEqual(r.util, 0.0)
        self.assertIsNone(r.reset_ts)
        self.assertFalse(r.will_exhaust_before_reset)


class TestExchange(unittest.TestCase):
    @staticmethod
    def pairs(*points):
        """(ts, 5h util, 5h reset, weekly util) to matched row lists."""
        return ([(t, fh, r) for t, fh, r, _ in points], [(t, wk, FUTURE) for t, _, _, wk in points])

    def test_ratio_skips_reset_steps(self):
        first, second = iso(300 * MIN), iso(600 * MIN)
        short, weekly = self.pairs((0, 0.0, first, 10.0), (200 * MIN, 30.0, first, 13.0),
                                   (310 * MIN, 0.0, second, 13.0), (400 * MIN, 20.0, second, 15.0))
        self.assertAlmostEqual(exchange(short, weekly), 10.0)  # 30 + 20 of 5h for 3 + 2 of weekly

    def test_too_little_weekly_rise_gives_none(self):
        short, weekly = self.pairs((0, 0.0, FUTURE, 10.0), (60 * MIN, 9.0, FUTURE, 10.5))
        self.assertIsNone(exchange(short, weekly))

    def test_stale_redraw_not_counted_twice(self):
        short, weekly = self.pairs((0, 10.0, FUTURE, 10.0), (MIN, 30.0, FUTURE, 12.0),
                                   (2 * MIN, 20.0, FUTURE, 11.0), (3 * MIN, 30.0, FUTURE, 12.0))
        self.assertAlmostEqual(exchange(short, weekly), 10.0)

    def test_idle_window_without_reset_rebaselines(self):
        short = [(0, 0.0, FUTURE), (MIN, 25.0, FUTURE), (2 * MIN, 0.0, None), (3 * MIN, 20.0, FUTURE)]
        weekly = [(t, wk, FUTURE) for t, wk in ((0, 10.0), (MIN, 15.0), (2 * MIN, 15.0), (3 * MIN, 19.0))]
        self.assertAlmostEqual(exchange(short, weekly), 5.0)  # 25 + 20 for 5 + 4

    def test_polls_preferred_over_redraws(self):
        short = [(0, 0.0, FUTURE, "poll"), (MIN, 30.0, FUTURE, "statusline"), (2 * MIN, 20.0, FUTURE, "poll")]
        weekly = [(0, 10.0, FUTURE, "poll"), (MIN, 11.0, FUTURE, "statusline"), (2 * MIN, 14.0, FUTURE, "poll")]
        self.assertAlmostEqual(exchange(short, weekly), 5.0)

    def test_statusline_redraw_skipped_without_polls(self):
        short = [(0, 0.0, FUTURE, "statusline"), (MIN, 30.0, FUTURE, "statusline"),
                 (2 * MIN, 10.0, FUTURE, "statusline"), (3 * MIN, 35.0, FUTURE, "statusline")]
        weekly = [(0, 10.0, FUTURE), (MIN, 13.0, FUTURE), (2 * MIN, 11.0, FUTURE), (3 * MIN, 13.5, FUTURE)]
        self.assertAlmostEqual(exchange(short, weekly), 10.0)  # 35 for 3.5

    def test_moving_weekly_reset_rebaselines(self):
        short = [(0, 0.0, FUTURE), (MIN, 10.0, FUTURE), (2 * MIN, 90.0, FUTURE), (3 * MIN, 100.0, FUTURE)]
        weekly = [(0, 10.0, iso(WEEK_MS)), (MIN, 12.0, iso(WEEK_MS)),
                  (2 * MIN, 40.0, iso(WEEK_MS - 86_400_000)), (3 * MIN, 42.0, iso(WEEK_MS - 86_400_000))]
        self.assertAlmostEqual(exchange(short, weekly), 5.0)


class TestPaced(unittest.TestCase):
    DAY = 1440 * MIN

    def test_burns_at_average_since_window_opened(self):
        reset = 5 * self.DAY  # opened 2 days before t=0
        rows = [(0, 20.0, iso(reset)), (self.DAY - MIN, 29.0, iso(reset)), (self.DAY, 30.0, iso(reset))]
        r = paced(compute_ok("seven_day", rows, self.DAY), rows, self.DAY)
        self.assertAlmostEqual(r.per_day, 10.0)
        self.assertAlmostEqual(r.runway_min, 7 * self.DAY / MIN)
        self.assertFalse(r.will_exhaust_before_reset)

    def test_early_week_leans_on_previous_week(self):
        start = WEEK_MS
        old = [(start - 60 * MIN, 70.0, iso(start))]
        new = [(start + 60 * MIN, 5.0, iso(start + WEEK_MS))]
        rows = old + new
        r = paced(compute_ok("seven_day", rows, start + 6 * 60 * MIN), rows, start + 6 * 60 * MIN)
        own, prev = 5.0 / 360, 70.0 / (WEEK_MS / MIN)
        self.assertAlmostEqual(r.per_min, own / 8 + prev * 7 / 8)

    def test_prior_found_when_week_opens_after_last_reset(self):
        start = WEEK_MS
        old = [(start - 1440 * MIN, 70.0, iso(start - 1440 * MIN))]  # reset a day before this week opened
        new = [(start + 60 * MIN, 5.0, iso(start + WEEK_MS))]
        r = paced(compute_ok("seven_day", old + new, start + 6 * 60 * MIN), old + new, start + 6 * 60 * MIN)
        self.assertAlmostEqual(r.per_min, 5.0 / 360 / 8 + 70.0 / (WEEK_MS / MIN) * 7 / 8)

    def test_month_opens_one_calendar_month_before_reset(self):
        reset = iso(datetime(2026, 3, 31, tzinfo=timezone.utc).timestamp() * 1000)
        opened = datetime(2026, 2, 28, tzinfo=timezone.utc).timestamp() * 1000
        now = opened + 14 * 1440 * MIN
        rows = [(now - MIN, 28.0, reset)]
        r = paced(compute_ok("ollama/allowance", rows, now), rows, now, "month")
        self.assertAlmostEqual(r.per_day, 2.0)

    def test_reset_window_untouched(self):
        rows = [(0, 60.0, iso(100 * MIN))]
        r = compute_ok("seven_day", rows, 101 * MIN)
        self.assertIs(paced(r, rows, 101 * MIN), r)

if __name__ == "__main__":
    unittest.main()
