import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import adapters, cli, config, state


class TtyInput(io.StringIO):
    def isatty(self):
        return True


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "real").mkdir()
        (root / "config.json").write_text(json.dumps({
            "select": "auto", "threshold": 90, "alimit": "/bin/alimit",
            "agents": {
                "claude": {"realDir": str(root / "real"), "bin": "/x/claude",
                           "accounts": {"work": {"mode": "shared"}}},
                "ollama": {"accounts": {"home": {}}},
                "opencode": {"realDir": "", "bin": "/x/opencode", "accounts": {}},
            },
        }))
        self.env = mock.patch.dict(os.environ, {
            "AI_ACCOUNTS_CONFIG": str(root / "config.json"),
            "AI_ACCOUNTS_STATE": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
        })
        self.env.start()
        os.environ.pop("AI_ACCOUNT_CLAUDE", None)
        os.environ.pop("ALIMIT_DB", None)
        self.cfg = config.load()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def login(self, agent, name, file, text="{}"):
        p = config.state_dir() / agent / name / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_auto_pick_calls_alimit_with_logged_in(self):
        self.login("claude", "work", ".credentials.json")
        done = mock.Mock(returncode=0, stdout='{"pick": "work"}')
        with mock.patch.object(cli.subprocess, "run", return_value=done) as run:
            self.assertEqual(cli.auto_pick(self.cfg, "claude", self.cfg.accounts["claude"]), "work")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["/bin/alimit", "headroom", "--agent", "claude"])
        self.assertEqual(argv[argv.index("--eligible") + 1], "default,work")

    def test_auto_pick_without_alimit(self):
        self.cfg.alimit = ""
        self.assertIsNone(cli.auto_pick(self.cfg, "claude", self.cfg.accounts["claude"]))

    def test_build_env_tags_and_pinned_db(self):
        acct = self.cfg.accounts["claude"]["work"]
        env = cli.build_env(self.cfg, "claude", acct, adapters.Launch({"CLAUDE_CONFIG_DIR": "/a"}, []), dict(os.environ))
        self.assertEqual(env["AI_ACCOUNT"], "work")
        self.assertEqual(env["AI_ACCOUNT_AGENT"], "claude")
        self.assertIn("claude=work", env["AI_ACCOUNT_TAGS"])
        self.assertEqual(env["ALIMIT_DB"], str(Path(os.environ["XDG_DATA_HOME"]) / "alimit/usage.db"))

    def test_build_env_composes_ollama_for_prime_agent(self):
        self.login("ollama", "home", "key", "sk-h")
        state.set_sticky("ollama", "home")
        acct = config.Account("prime-agent", "default")
        env = cli.build_env(self.cfg, "prime-agent", acct, adapters.Launch({}, []), dict(os.environ))
        self.assertEqual(env["OLLAMA_API_KEY"], "sk-h")
        self.assertIn("ollama=home", env["AI_ACCOUNT_TAGS"])

    def test_exec_without_accounts_passes_through(self):
        with mock.patch.object(cli.os, "execvp") as ex:
            cli.main(["exec", "antigravity", "--", "/x/agy", "run"])
        ex.assert_called_once_with("/x/agy", ["/x/agy", "run"])

    def test_exec_unknown_account_exit_2(self):
        self.assertEqual(cli.main(["exec", "claude", "--account", "nope", "--", "/x/claude"]), 2)

    def test_exec_logged_out_exit_3(self):
        # No tty: no way to sign in mid-launch, so keep failing.
        with mock.patch.object(cli.sys, "stdin", io.StringIO()):
            self.assertEqual(cli.main(["exec", "claude", "--account", "work", "--", "/x/claude"]), 3)

    def test_exec_logged_out_tty_launches_and_gives_the_login_hint(self):
        err = io.StringIO()
        with mock.patch.object(cli.sys, "stdin", TtyInput()), \
             mock.patch.object(cli.os, "execvpe") as ex, redirect_stderr(err):
            cli.main(["exec", "claude", "--account", "work", "--", "/x/claude"])
        ex.assert_called_once()
        self.assertIn("claude/work is not logged in; sign in when the agent asks", err.getvalue())

    def test_exec_tty_prompts_for_the_key_a_dependent_ollama_account_needs(self):
        # prime-agent carries an ollama tag, so a keyless ollama account must ask
        # here instead of failing once the agent is already up.
        self.cfg.accounts["prime-agent"] = {"work": config.Account("prime-agent", "work")}
        self.cfg.real_dirs["prime-agent"] = str(Path(self.tmp.name) / "real")
        self.login("prime-agent", "work", "auth.json")
        state.set_sticky("ollama", "home")
        with mock.patch.object(cli.config, "load", return_value=self.cfg), \
             mock.patch.object(cli.sys, "stdin", TtyInput()), \
             mock.patch.object(cli.getpass, "getpass", return_value="sk-home") as ask, \
             mock.patch.object(cli.os, "execvpe") as ex:
            cli.main(["exec", "prime-agent", "--account", "work", "--", "/x/prime-agent"])
        ask.assert_called_once()
        ex.assert_called_once()
        self.assertEqual(ex.call_args.args[2]["OLLAMA_API_KEY"], "sk-home")
        self.assertIn("ollama=home", ex.call_args.args[2]["AI_ACCOUNT_TAGS"])
        key = config.state_dir() / "ollama" / "home" / "key"
        self.assertEqual(key.read_text(), "sk-home\n")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_exec_without_tty_still_exits_3_when_the_ollama_key_is_missing(self):
        self.cfg.accounts["prime-agent"] = {"work": config.Account("prime-agent", "work")}
        self.cfg.real_dirs["prime-agent"] = str(Path(self.tmp.name) / "real")
        self.login("prime-agent", "work", "auth.json")
        state.set_sticky("ollama", "home")
        with mock.patch.object(cli.config, "load", return_value=self.cfg), \
             mock.patch.object(cli.sys, "stdin", io.StringIO()):
            self.assertEqual(
                cli.main(["exec", "prime-agent", "--account", "work", "--", "/x/prime-agent"]), 3)

    def test_exec_logged_out_tty_prompts_for_the_ollama_key(self):
        with mock.patch.object(cli.sys, "stdin", TtyInput()), \
             mock.patch.object(cli.getpass, "getpass", return_value="sk-x") as ask, \
             mock.patch.object(cli.os, "execvpe") as ex:
            cli.main(["exec", "ollama", "--account", "home", "--", "/x/ollama"])
        ask.assert_called_once()
        ex.assert_called_once()
        key = config.state_dir() / "ollama" / "home" / "key"
        self.assertEqual(key.read_text(), "sk-x\n")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ex.call_args.args[2]["OLLAMA_API_KEY"], "sk-x")

    def test_exec_logged_out_tty_runs_the_opencode_login_first(self):
        acct = config.Account("opencode", "work")
        self.cfg.accounts["opencode"] = {"work": acct}

        def login(argv, **kw):
            auth = acct.dir / "opencode" / "auth.json"
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text("{}")
            return mock.Mock(returncode=0)

        with mock.patch.object(cli.config, "load", return_value=self.cfg), \
             mock.patch.object(cli, "_headroom", return_value=None), \
             mock.patch.object(cli.sys, "stdin", TtyInput()), \
             mock.patch.object(cli.subprocess, "run", side_effect=login) as run, \
             mock.patch.object(cli.os, "execvpe") as ex:
            cli.main(["exec", "opencode", "--account", "work", "--", "/x/opencode"])
        self.assertEqual(run.call_args.args[0], ["/x/opencode", "auth", "login"])
        ex.assert_called_once()

    def test_exec_sets_env_and_status(self):
        self.login("claude", "work", ".credentials.json")
        with mock.patch.object(cli.os, "execvpe") as ex:
            cli.main(["exec", "claude", "--account", "work", "--", "/x/claude", "-c"])
        argv0, argv, env = ex.call_args.args
        self.assertEqual(argv, ["/x/claude", "-c"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(config.state_dir() / "claude" / "work"))
        self.assertEqual(state.status()["claude"], {"default": True, "work": True})

    def test_use_sets_sticky_and_validates(self):
        self.assertEqual(cli.main(["use", "claude", "work"]), 0)
        self.assertEqual(state.sticky()["claude"], "work")
        self.assertEqual(cli.main(["use", "claude", "auto"]), 0)
        self.assertEqual(cli.main(["use", "claude", "nope"]), 2)

    def test_list_json(self):
        self.login("claude", "work", ".credentials.json")
        state.set_sticky("claude", "work")
        with mock.patch.object(cli, "_headroom", return_value={"pick": None, "accounts": [
            {"account": "default", "max_util": 50.0, "reset_ts": None},
            {"account": "work", "max_util": 5.0, "reset_ts": 9}]}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["list", "--json"])
        rows = {r["account"]: r for r in json.loads(buf.getvalue())["claude"]}
        self.assertTrue(rows["work"]["sticky"])
        self.assertEqual(rows["work"]["maxUtil"], 5.0)
        self.assertTrue(rows["work"]["loggedIn"])
