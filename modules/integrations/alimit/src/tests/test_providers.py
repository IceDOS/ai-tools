import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from alimit import cli, config, poller, providers, sources, store


class TestParsers(unittest.TestCase):
    def test_opencode_go(self):
        data = {"usage": {
            "rolling": {"status": "ok", "percent": 98, "resetsAt": "2026-10-07T22:03:52.000Z"},
            "weekly": {"status": "ok", "percent": 39, "resetsAt": "2026-10-12T00:00:00.000Z"},
            "monthly": "bogus",
        }}
        out = providers.parse_opencode_go(data)
        self.assertEqual(out["opencode-go/rolling"], {"util": 98.0, "resets_at": "2026-10-07T22:03:52.000Z"})
        self.assertEqual(set(out), {"opencode-go/rolling", "opencode-go/weekly"})

    def test_ollama_allowance_and_windows(self):
        data = {"included": {
            "balance_usd": 1.5, "allowance_usd": 2.5,
            "period": {"from": "2026-09-09T07:29:06Z", "until": "2026-10-09T07:29:06Z"},
            "session": {"remaining_percent": 70, "resets_at": "2026-10-09T03:00:00Z"},
        }}
        out = providers.parse_ollama(data)
        self.assertAlmostEqual(out["ollama/allowance"]["util"], 40.0)
        self.assertEqual(out["ollama/allowance"]["resets_at"], "2026-10-09T07:29:06Z")
        self.assertEqual(out["ollama/session"]["util"], 30.0)

    def test_openrouter_free_daily_and_credits(self):
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        data = {"data": {
            "limit": 10, "limit_remaining": 7.5, "limit_reset": "monthly",
            "free_model_daily_requests": {"used": 10, "limit": 50, "remaining": 40},
        }}
        out = providers.parse_openrouter(data, now)
        self.assertEqual(out["openrouter/free_daily"]["util"], 20.0)
        self.assertEqual(out["openrouter/free_daily"]["resets_at"], "2026-10-10T00:00:00+00:00")
        self.assertEqual(out["openrouter/credits"]["util"], 25.0)
        self.assertEqual(out["openrouter/credits"]["resets_at"], "2026-11-01T00:00:00+00:00")

    def test_openrouter_unlimited_key_has_no_credit_window(self):
        out = providers.parse_openrouter({"data": {"limit": None, "limit_remaining": None}})
        self.assertEqual(out, {})

    def test_antigravity_payload(self):
        now_ms = 1_800_000_000_000
        payload = {"quota": {"gemini-5h": {"remaining_fraction": 0.25, "reset_in_seconds": 3601}}}
        out = providers.parse_antigravity(payload, now_ms)
        self.assertEqual(out["antigravity/gemini-5h"]["util"], 75.0)
        # rounded to the minute so redraw jitter writes no new rows
        reset = datetime.fromisoformat(out["antigravity/gemini-5h"]["resets_at"])
        self.assertEqual(reset.timestamp() % 60, 0)


class TestLabels(unittest.TestCase):
    def test_provider_labels(self):
        self.assertEqual(config.label("antigravity/gemini-5h"), "Antigravity · Gemini · 5-hour")
        self.assertEqual(config.short_label("antigravity/gemini-5h"), "ag gem 5h")
        self.assertEqual(config.label("openrouter/free_daily"), "OpenRouter · free requests today")
        self.assertEqual(config.short_label("ollama/allowance"), "ol mo")

    def test_provider_of_keeps_claude_scoped_keys(self):
        self.assertEqual(config.provider_of("weekly_scoped:claude_opus_4.5"), "claude")
        self.assertEqual(config.provider_of("five_hour"), "claude")
        self.assertEqual(config.provider_of("ollama/session"), "ollama")
        self.assertFalse(config.is_known_window("nimbus/thing"))


class TestKeys(unittest.TestCase):
    def test_tries_each_key_and_raises_entitlement(self):
        calls = []

        def fake_get(url, key):
            calls.append(key)
            if key == "stale":
                raise sources.FetchError("HTTP 401", status=401)
            raise sources.FetchError("HTTP 403", status=403,
                                     body={"error": {"type": "EntitlementError"}})

        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"opencode-go": {"type": "api", "key": "stale"},
                                        "opencode": {"type": "api", "key": "valid"}}))
            with mock.patch.object(config, "PRIME_AGENT_AUTH", auth), \
                    mock.patch.object(config, "OPENCODE_AUTH", Path(tmp) / "none.json"), \
                    mock.patch.object(providers, "_get", side_effect=fake_get):
                with self.assertRaises(providers.NotSubscribed):
                    providers.fetch_opencode_go()
        self.assertEqual(calls, ["stale", "valid"])

    def test_no_keys(self):
        with mock.patch.object(config, "PRIME_AGENT_AUTH", Path("/nonexistent")), \
                mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(providers.NoCredentials):
                providers.fetch_ollama()


class TestPollProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_records_then_throttles(self):
        windows = {"ollama/allowance": {"util": 5.0, "resets_at": None}}
        with mock.patch.dict(providers.POLLERS, {"ollama": lambda: windows}):
            self.assertEqual(poller.poll_provider(self.con, "ollama", 1_000_000), ("poll", 1))
            self.assertEqual(poller.poll_provider(self.con, "ollama", 1_060_000)[0], "throttled")

    def test_missing_plan_waits_long(self):
        def no_plan():
            raise providers.NotSubscribed("no plan")

        with mock.patch.dict(providers.POLLERS, {"opencode-go": no_plan}):
            self.assertTrue(poller.poll_provider(self.con, "opencode-go", 0)[0].startswith("skipped"))
        self.assertEqual(store.get_meta_int(self.con, "opencode-go_next_ms"), poller.IDLE_RECHECK_S * 1000)


class TestCollectFilter(unittest.TestCase):
    def test_orders_claude_first_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(Path(tmp) / "u.db")
            now = 1_800_000_000_000
            store.record(con, now - 1000, {
                "openrouter/free_daily": {"util": 20.0, "resets_at": None},
                "antigravity/gemini-5h": {"util": 30.0, "resets_at": None},
                "five_hour": {"util": 40.0, "resets_at": None},
            }, "poll")
            self.assertEqual([r.window for r in cli.collect(con, now)],
                             ["five_hour", "antigravity/gemini-5h", "openrouter/free_daily"])
            self.assertEqual([r.window for r in cli.collect(con, now, providers={"antigravity"})],
                             ["antigravity/gemini-5h"])
            con.close()


if __name__ == "__main__":
    unittest.main()
