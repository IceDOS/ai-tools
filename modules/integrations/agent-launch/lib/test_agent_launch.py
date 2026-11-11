import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import agent_launch as al

AGENTS = {
    "claude": {"command": ["claude"], "label": "Claude Code"},
    "pi-agent": {"command": ["pi-agent"], "label": ""},
}


class ChooseTest(unittest.TestCase):
    def test_default_agent_skips_picker(self):
        cfg = {"defaultAgent": "claude", "agents": AGENTS}
        picker = mock.Mock()
        self.assertEqual(al.choose(cfg, picker), ("claude", ["claude"]))
        picker.assert_not_called()

    def test_empty_default_prompts_picker(self):
        cfg = {"defaultAgent": "", "agents": AGENTS}
        self.assertEqual(al.choose(cfg, lambda c: "pi-agent"), ("pi-agent", ["pi-agent"]))

    def test_unknown_default_prompts_picker(self):
        cfg = {"defaultAgent": "removed", "agents": AGENTS}
        self.assertEqual(al.choose(cfg, lambda c: "claude")[0], "claude")

    def test_cancelled_picker_returns_none(self):
        cfg = {"defaultAgent": "", "agents": AGENTS}
        self.assertEqual(al.choose(cfg, lambda c: None), (None, None))

    def test_agent_without_command_runs_its_name(self):
        cfg = {"defaultAgent": "x", "agents": {"x": {"command": []}}}
        self.assertEqual(al.choose(cfg), ("x", ["x"]))

    def test_label_falls_back_to_title_case(self):
        self.assertEqual(al.label("pi-agent", AGENTS["pi-agent"]), "Pi Agent")
        self.assertEqual(al.label("claude", AGENTS["claude"]), "Claude Code")

    def test_single_agent_skips_fzf(self):
        with mock.patch.object(al.subprocess, "run") as run:
            self.assertEqual(al.pick_agent({"agents": {"only": {}}}), "only")
        run.assert_not_called()


class MainTest(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump({"defaultAgent": "pi-agent", "agents": AGENTS}, tmp)
        self.cfg = tmp.name
        self.addCleanup(Path(tmp.name).unlink)

    def test_print_emits_choice(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(al.main(["--config", self.cfg, "--print"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"agent": "pi-agent", "argv": ["pi-agent"], "account": None, "env": {}})

    def test_launch_execs_agent(self):
        with mock.patch.object(al.os, "execvp") as execvp:
            al.main(["--config", self.cfg])
        execvp.assert_called_once_with("pi-agent", ["pi-agent"])

    def test_agent_flag_auto_account_and_extra_args(self):
        with mock.patch.object(al, "pick_account") as pick, mock.patch.object(al.os, "execvp") as execvp:
            al.main(["--config", self.cfg, "--agent", "claude", "--auto-account", "--", "-p", "hi"])
        pick.assert_not_called()
        execvp.assert_called_once_with("claude", ["claude", "-p", "hi"])

    def test_unknown_agent_flag_fails(self):
        with redirect_stdout(io.StringIO()), mock.patch.object(al.sys, "stderr", io.StringIO()):
            self.assertEqual(al.main(["--config", self.cfg, "--agent", "nope"]), 1)


if __name__ == "__main__":
    unittest.main()


ROWS = [
    {"account": "default", "label": "default", "loggedIn": True, "maxUtil": 80.0, "windows": {"5h": 30.0, "wk": 80.0, "mo": 5.0}, "sticky": False, "next": False},
    {"account": "work", "label": "Work", "loggedIn": True, "maxUtil": 10.0, "windows": {"5h": None, "wk": 10.0, "mo": None}, "sticky": False, "next": True},
]


class AccountStepTest(unittest.TestCase):
    def test_no_accounts_skips(self):
        self.assertIsNone(al.pick_account("claude", lister=lambda n: [], picker=mock.Mock()))

    def test_single_account_skips(self):
        picker = mock.Mock()
        self.assertIsNone(al.pick_account("claude", lister=lambda n: ROWS[:1], picker=picker))
        picker.assert_not_called()

    def test_picker_lines_and_choice(self):
        seen = {}

        def picker(lines):
            seen["lines"] = lines
            return lines[1]

        self.assertEqual(al.pick_account("claude", lister=lambda n: ROWS, picker=picker), "work")
        self.assertIn("5h  30%  wk  80%  mo   5%", seen["lines"][0])
        self.assertIn("5h    \u2014  wk  10%  mo    \u2014", seen["lines"][1])
        self.assertIn("(auto)", seen["lines"][1])

    def test_print_includes_account_env(self):
        cfg = {"defaultAgent": "claude", "agents": AGENTS}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
        buf = io.StringIO()
        with mock.patch.object(al, "pick_account", return_value="work"), redirect_stdout(buf):
            al.main(["--config", f.name, "--print"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["account"], "work")
        self.assertEqual(out["env"], {"AI_ACCOUNT_CLAUDE": "work"})


def test_relay_env_off_by_default():
    assert al.relay_env({}) == {}


def test_relay_env_generates_five_hex():
    env = al.relay_env({"relayIds": True})
    assert re.fullmatch(r"[0-9a-f]{5}", env["AGENT_RELAY_ID"])


def test_relay_env_uses_generator():
    assert al.relay_env({"relayIds": True}, gen=lambda: "3f2a9") == {"AGENT_RELAY_ID": "3f2a9"}


def test_relay_title():
    assert al.relay_title("claude", "3f2a9") == "\033]0;claude · 3f2a9\007"


def test_print_includes_relay_id(tmp_path, capsys):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"relayIds": True, "agents": {"pi": {"command": ["pi"]}}}))
    assert al.main(["--config", str(cfg), "--agent", "pi", "--auto-account", "--print"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert re.fullmatch(r"[0-9a-f]{5}", out["env"]["AGENT_RELAY_ID"])

