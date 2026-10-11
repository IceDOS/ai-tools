import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import adapters
from ai_accounts.config import Account, Config


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "AI_ACCOUNTS_STATE": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/real/bus",
        })
        self.env.start()
        self.real = root / "real"
        self.real.mkdir()
        (self.real / "settings.json").write_text("{}")
        self.cfg = Config(real_dirs={a: str(self.real) for a in ("claude", "prime-agent", "antigravity")},
                          bins={"claude": "/x/claude", "opencode": "/x/opencode", "antigravity": "/x/agy",
                                "prime-agent": "/x/prime-agent"},
                          bus_talk=["org.kde.*"])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_default_account_changes_nothing(self):
        launch = adapters.prepare(Account("claude", "default"), self.cfg)
        self.assertEqual((launch.env, launch.args), ({}, []))

    def test_claude_shared(self):
        (self.real / "projects").mkdir()
        acct = Account("claude", "work")
        launch = adapters.prepare(acct, self.cfg)
        self.assertEqual(launch.env, {"CLAUDE_CONFIG_DIR": str(acct.dir)})
        self.assertTrue((acct.dir / "projects").is_symlink())

    def test_claude_isolated_has_no_links(self):
        acct = Account("claude", "solo", "isolated")
        adapters.prepare(acct, self.cfg)
        self.assertFalse((acct.dir / "settings.json").exists())

    def test_prime_agent_private_daemon_dirs(self):
        (self.real / "daemon-workers").mkdir()
        acct = Account("prime-agent", "work")
        launch = adapters.prepare(acct, self.cfg)
        self.assertEqual(launch.env["PRIME_AGENT_CODING_AGENT_DIR"], str(acct.dir))
        self.assertFalse((acct.dir / "daemon-workers").exists())

    def test_opencode_shared_passes_auth_content(self):
        acct = Account("opencode", "work")
        auth = acct.dir / "opencode" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"openrouter": {"type": "api", "key": "k"}}')
        launch = adapters.prepare(acct, self.cfg)
        self.assertEqual(json.loads(launch.env["OPENCODE_AUTH_CONTENT"])["openrouter"]["key"], "k")
        self.assertNotIn("XDG_DATA_HOME", launch.env)

    def test_opencode_isolated_moves_data_home(self):
        acct = Account("opencode", "solo", "isolated")
        self.assertEqual(adapters.prepare(acct, self.cfg).env, {"XDG_DATA_HOME": str(acct.dir)})

    def test_opencode_oauth_detection(self):
        acct = Account("opencode", "work")
        auth = acct.dir / "opencode" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"anthropic": {"type": "oauth"}, "openrouter": {"type": "api"}}')
        self.assertEqual(adapters.opencode_oauth(acct), ["anthropic"])

    def test_ollama_key(self):
        acct = Account("ollama", "work")
        acct.dir.mkdir(parents=True)
        (acct.dir / "key").write_text("sk-1\n")
        self.assertEqual(adapters.prepare(acct, self.cfg).env, {"OLLAMA_API_KEY": "sk-1"})

    def test_ollama_without_key_not_logged_in(self):
        acct = Account("ollama", "work")
        self.assertFalse(adapters.logged_in(acct))
        with self.assertRaises(adapters.NotLoggedIn):
            adapters.prepare(acct, self.cfg)

    def test_logged_in_needs_nonempty_creds(self):
        acct = Account("claude", "work")
        acct.dir.mkdir(parents=True)
        (acct.dir / ".credentials.json").write_text("")
        self.assertFalse(adapters.logged_in(acct))
        (acct.dir / ".credentials.json").write_text("{}")
        self.assertTrue(adapters.logged_in(acct))

    def test_antigravity_isolated_uses_home_and_proxy(self):
        acct = Account("antigravity", "work", "isolated")
        with mock.patch.object(adapters.dbusproxy, "start", return_value=7) as start:
            launch = adapters.prepare(acct, self.cfg)
        home = acct.dir / "home"
        self.assertEqual(launch.env["HOME"], str(home))
        self.assertTrue(launch.env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path="))
        self.assertEqual(launch.keep_fds, (7,))
        self.assertTrue((home / "settings.json").is_symlink())
        self.assertTrue((home / ".gemini").is_dir())
        self.assertFalse((home / ".gemini").is_symlink())
        self.assertEqual(start.call_args.args[0], "unix:path=/real/bus")
        self.assertEqual(start.call_args.args[2], ["org.kde.*"])

    def test_antigravity_shared_keeps_token_private(self):
        gem = self.real / ".gemini"
        for rel in adapters.AGY_PRIVATE:
            (gem / rel).parent.mkdir(parents=True, exist_ok=True)
            (gem / rel).write_text("REAL")
        (gem / "settings.json").write_text("{}")
        acct = Account("antigravity", "work")
        with mock.patch.object(adapters.dbusproxy, "start", return_value=7):
            adapters.prepare(acct, self.cfg)
        home_gem = acct.dir / "home" / ".gemini"
        for rel in adapters.AGY_PRIVATE:
            self.assertFalse((home_gem / rel).exists())
        self.assertTrue((home_gem / "settings.json").is_symlink())

    def test_login_plans(self):
        argv, env, hint = adapters.login_plan(Account("opencode", "work"), self.cfg)
        self.assertEqual(argv, ["/x/opencode", "auth", "login"])
        self.assertEqual(env, {"XDG_DATA_HOME": str(Account("opencode", "work").dir)})
        argv, env, hint = adapters.login_plan(Account("claude", "work"), self.cfg)
        self.assertEqual(argv, ["/x/claude"])
        self.assertIn("/login", hint)
        argv, _, _ = adapters.login_plan(Account("ollama", "work"), self.cfg)
        self.assertIsNone(argv)

    def test_claude_settings_seeded_and_merged(self):
        (self.real / "settings.json").write_text('{"theme": "dark", "model": "sonnet"}')
        self.cfg.managed = {"claude": {"statusLine": {"command": "alimit"}}}
        acct = Account("claude", "work", settings={"model": "opus"})
        adapters.prepare(acct, self.cfg)
        f = acct.dir / "settings.json"
        self.assertFalse(f.is_symlink())
        self.assertEqual(json.loads(f.read_text()),
                         {"theme": "dark", "model": "opus", "statusLine": {"command": "alimit"}})
        self.assertEqual(json.loads((self.real / "settings.json").read_text())["model"], "sonnet")

    def test_antigravity_settings_and_owned_entries(self):
        cli = self.real / ".gemini" / "antigravity-cli"
        (cli / "log").mkdir(parents=True)
        (cli / "settings.json").write_text("{}")
        (cli / "cli.log").symlink_to("log/cli-1.log")
        self.cfg.managed = {"antigravity": {"statusLine": {"type": "command", "command": "bar"}}}
        acct = Account("antigravity", "work")
        with mock.patch.object(adapters.dbusproxy, "start", return_value=7):
            adapters.prepare(acct, self.cfg)
        own = acct.dir / "home" / ".gemini" / "antigravity-cli"
        self.assertEqual(json.loads((own / "settings.json").read_text())["statusLine"]["command"], "bar")
        self.assertFalse(os.path.lexists(own / "cli.log"))
        self.assertFalse(os.path.lexists(own / "log"))

    def test_opencode_settings_file_passed(self):
        acct = Account("opencode", "work", settings={"model": "x/y"})
        launch = adapters.prepare(acct, self.cfg)
        self.assertEqual(launch.env["OPENCODE_CONFIG"], str(acct.dir / "opencode-settings.json"))
        self.assertEqual(json.loads((acct.dir / "opencode-settings.json").read_text()), {"model": "x/y"})

    def test_no_settings_writes_nothing(self):
        acct = Account("opencode", "work")
        launch = adapters.prepare(acct, self.cfg)
        self.assertNotIn("OPENCODE_CONFIG", launch.env)
        self.assertFalse((acct.dir / adapters.LEDGER).exists())
