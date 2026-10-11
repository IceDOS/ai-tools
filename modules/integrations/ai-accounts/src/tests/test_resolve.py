import unittest

from ai_accounts.config import Account
from ai_accounts.resolve import UnknownAccount, env_var, resolve

ACCTS = {n: Account("claude", n) for n in ("default", "work", "home")}


class ResolveTest(unittest.TestCase):
    def test_no_accounts_is_default(self):
        self.assertEqual(resolve("claude", {}, flag="work"), "default")

    def test_flag_beats_everything(self):
        env = {env_var("claude"): "home"}
        self.assertEqual(resolve("claude", ACCTS, flag="work", environ=env, sticky="home"), "work")

    def test_env_beats_sticky(self):
        self.assertEqual(resolve("claude", ACCTS, environ={"AI_ACCOUNT_CLAUDE": "home"}, sticky="work"), "home")

    def test_sticky_beats_auto(self):
        self.assertEqual(resolve("claude", ACCTS, environ={}, sticky="work", select="auto", auto=lambda: "home"), "work")

    def test_auto_when_select_auto(self):
        self.assertEqual(resolve("claude", ACCTS, environ={}, select="auto", auto=lambda: "home"), "home")

    def test_sticky_auto_enables_auto_under_manual(self):
        self.assertEqual(resolve("claude", ACCTS, environ={}, sticky="auto", auto=lambda: "work"), "work")

    def test_auto_none_falls_back_to_default(self):
        self.assertEqual(resolve("claude", ACCTS, environ={}, select="auto", auto=lambda: None), "default")

    def test_manual_ignores_auto(self):
        self.assertEqual(resolve("claude", ACCTS, environ={}, select="manual", auto=lambda: "work"), "default")

    def test_unknown_flag_raises(self):
        with self.assertRaises(UnknownAccount) as cm:
            resolve("claude", ACCTS, flag="nope", environ={})
        self.assertEqual(cm.exception.valid, ["default", "home", "work"])

    def test_env_var_name(self):
        self.assertEqual(env_var("prime-agent"), "AI_ACCOUNT_PRIME_AGENT")
