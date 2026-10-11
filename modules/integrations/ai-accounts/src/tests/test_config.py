import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import config, state


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "AI_ACCOUNTS_CONFIG": str(self.root / "config.json"),
            "AI_ACCOUNTS_STATE": str(self.root / "state"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, data):
        (self.root / "config.json").write_text(json.dumps(data))

    def test_missing_file_is_empty(self):
        cfg = config.load()
        self.assertEqual(cfg.accounts, {})
        self.assertEqual(cfg.select, "manual")

    def test_declared_accounts_add_default(self):
        self.write({
            "select": "auto", "threshold": 80, "busTalk": ["org.kde.*"], "alimit": "/bin/alimit",
            "agents": {"claude": {"realDir": "/h/.claude", "bin": "/x/claude",
                                  "accounts": {"work": {"mode": "isolated", "label": "Work"}}}},
        })
        cfg = config.load()
        self.assertEqual(sorted(cfg.accounts["claude"]), ["default", "work"])
        work = cfg.accounts["claude"]["work"]
        self.assertEqual((work.mode, work.label), ("isolated", "Work"))
        self.assertEqual(work.dir, self.root / "state" / "claude" / "work")
        self.assertTrue(cfg.accounts["claude"]["default"].is_default)
        self.assertEqual(cfg.real_dirs["claude"], "/h/.claude")
        self.assertEqual(cfg.bins["claude"], "/x/claude")
        self.assertEqual((cfg.select, cfg.threshold, cfg.bus_talk), ("auto", 80, ["org.kde.*"]))

    def test_agent_without_accounts_has_none(self):
        self.write({"agents": {"opencode": {"realDir": "/d", "accounts": {}}}})
        self.assertEqual(config.load().accounts["opencode"], {})

    def test_alimit_db_ignores_later_xdg_change(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/real"}, clear=False):
            os.environ.pop("ALIMIT_DB", None)
            self.assertEqual(config.alimit_db(), Path("/real/alimit/usage.db"))
        with mock.patch.dict(os.environ, {"ALIMIT_DB": "/pinned.db"}):
            self.assertEqual(config.alimit_db(), Path("/pinned.db"))


    def test_settings_loaded(self):
        self.write({"agents": {"claude": {
            "settings": {"statusLine": {"command": "alimit"}},
            "accounts": {"work": {"settings": {"model": "opus"}}},
        }}})
        cfg = config.load()
        self.assertEqual(cfg.managed["claude"], {"statusLine": {"command": "alimit"}})
        self.assertEqual(cfg.accounts["claude"]["work"].settings, {"model": "opus"})
        self.assertEqual(cfg.accounts["claude"]["default"].settings, {})


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AI_ACCOUNTS_STATE": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_sticky_roundtrip(self):
        self.assertEqual(state.sticky(), {})
        state.set_sticky("claude", "work")
        state.set_sticky("ollama", "auto")
        self.assertEqual(state.sticky(), {"claude": "work", "ollama": "auto"})

    def test_status_merges_per_agent(self):
        state.write_status("claude", {"default": True, "work": False})
        state.write_status("ollama", {"default": True})
        self.assertEqual(state.status()["claude"], {"default": True, "work": False})
        self.assertIn("ollama", state.status())
