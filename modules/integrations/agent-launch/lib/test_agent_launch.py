import io
import json
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
    "prime-agent": {"command": ["prime-agent"], "label": ""},
}


class ChooseTest(unittest.TestCase):
    def test_default_agent_skips_picker(self):
        cfg = {"defaultAgent": "claude", "agents": AGENTS}
        picker = mock.Mock()
        self.assertEqual(al.choose(cfg, picker), ("claude", ["claude"]))
        picker.assert_not_called()

    def test_empty_default_prompts_picker(self):
        cfg = {"defaultAgent": "", "agents": AGENTS}
        self.assertEqual(al.choose(cfg, lambda c: "prime-agent"), ("prime-agent", ["prime-agent"]))

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
        self.assertEqual(al.label("prime-agent", AGENTS["prime-agent"]), "Prime Agent")
        self.assertEqual(al.label("claude", AGENTS["claude"]), "Claude Code")

    def test_single_agent_skips_fzf(self):
        with mock.patch.object(al.subprocess, "run") as run:
            self.assertEqual(al.pick_agent({"agents": {"only": {}}}), "only")
        run.assert_not_called()


class MainTest(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump({"defaultAgent": "prime-agent", "agents": AGENTS}, tmp)
        self.cfg = tmp.name
        self.addCleanup(Path(tmp.name).unlink)

    def test_print_emits_choice(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(al.main(["--config", self.cfg, "--print"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"agent": "prime-agent", "argv": ["prime-agent"]})

    def test_launch_execs_agent(self):
        with mock.patch.object(al.os, "execvp") as execvp:
            al.main(["--config", self.cfg])
        execvp.assert_called_once_with("prime-agent", ["prime-agent"])


if __name__ == "__main__":
    unittest.main()
