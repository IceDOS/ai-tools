import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alimit import accounts


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.json").write_text(json.dumps({"agents": {
            "claude": {"accounts": {"work": {"mode": "shared"}}},
            "opencode": {"accounts": {"solo": {"mode": "isolated"}}},
            "ollama": {"accounts": {}},
        }}))
        self.env = mock.patch.dict(os.environ, {
            "AI_ACCOUNTS_CONFIG": str(root / "config.json"),
            "AI_ACCOUNTS_STATE": str(root / "state"),
        })
        self.env.start()
        self.root = root

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_declared(self):
        got = sorted((a.agent, a.name, a.tag) for a in accounts.declared())
        self.assertEqual(got, [("claude", "work", "work"), ("opencode", "solo", "opencode.solo")])

    def test_credentials_paths(self):
        st = self.root / "state"
        self.assertEqual(accounts.Account("claude", "work").credentials, st / "claude/work/.credentials.json")
        self.assertEqual(accounts.Account("opencode", "solo").credentials, st / "opencode/solo/opencode/auth.json")
        self.assertEqual(accounts.Account("prime-agent", "w").credentials, st / "prime-agent/w/auth.json")
        self.assertEqual(accounts.Account("ollama", "h").credentials, st / "ollama/h/key")

    def test_env_tags(self):
        with mock.patch.dict(os.environ, {"AI_ACCOUNT_TAGS": "claude=work,ollama=default"}):
            self.assertEqual(accounts.env_tags(), {"claude": "work", "ollama": "default"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_ACCOUNT_TAGS", None)
            self.assertIsNone(accounts.env_tags())
from alimit import store


class RankTest(unittest.TestCase):
    H = accounts.Headroom

    def test_lowest_under_threshold(self):
        rows = [self.H("default", 95.0, 10), self.H("work", 40.0, 50), self.H("home", 20.0, 90)]
        self.assertEqual(accounts.rank(rows, 90, ["default", "work", "home"]), "home")

    def test_tie_keeps_eligible_order(self):
        rows = [self.H("work", 0.0, None), self.H("home", 0.0, None)]
        self.assertEqual(accounts.rank(rows, 90, ["home", "work"]), "home")

    def test_all_over_threshold_soonest_reset(self):
        rows = [self.H("default", 95.0, 500), self.H("work", 99.0, 100)]
        self.assertEqual(accounts.rank(rows, 90, ["default", "work"]), "work")

    def test_ineligible_skipped(self):
        rows = [self.H("work", 1.0, None), self.H("default", 50.0, None)]
        self.assertEqual(accounts.rank(rows, 90, ["default"]), "default")

    def test_empty(self):
        self.assertIsNone(accounts.rank([], 90, []))


class HeadroomTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")
        future = "2099-01-01T00:00:00+00:00"
        store.record(self.con, 1000, {
            "five_hour": {"util": 80.0, "resets_at": future},
            "seven_day": {"util": 30.0, "resets_at": future},
            "claude@work/five_hour": {"util": 10.0, "resets_at": future},
        }, "poll")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_max_util_per_account(self):
        rows = {r.account: r for r in accounts.headroom(self.con, "claude", ["default", "work", "new"], 2000)}
        self.assertEqual(rows["default"].max_util, 80.0)
        self.assertEqual(rows["work"].max_util, 10.0)
        self.assertEqual(rows["new"].max_util, 0.0)

    def test_agent_account(self):
        self.assertEqual(accounts.agent_account("claude@work/five_hour"), ("claude", "work"))
        self.assertEqual(accounts.agent_account("five_hour"), ("claude", "default"))
        self.assertEqual(accounts.agent_account("opencode-go@opencode.solo/rolling"), ("opencode", "solo"))
        self.assertIsNone(accounts.agent_account("opencode-go/rolling"))