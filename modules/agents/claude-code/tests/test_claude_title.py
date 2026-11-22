import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_MODULE_DIR = Path(__file__).resolve().parent.parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
_LIB_DIR = _MODULE_DIR.parent / "lib" / "terminal-title"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import terminal_title
import tool_state
import tool_timer


class TestClaudeTitleSource(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.transcript = Path(self.tmp_dir.name) / "session.jsonl"

    def test_custom_title_from_transcript(self):
        lines = [
            json.dumps({"type": "ai-title", "aiTitle": "AI Title"}),
            json.dumps({"type": "custom-title", "customTitle": "My Custom Title"}),
        ]
        self.transcript.write_text("\n".join(lines) + "\n")
        title = terminal_title.conversation_title("s1", str(self.transcript))
        self.assertEqual(title, "My Custom Title")

    def test_ai_title_fallback_from_transcript(self):
        lines = [
            json.dumps({"type": "ai-title", "aiTitle": "AI Generated Title"}),
        ]
        self.transcript.write_text("\n".join(lines) + "\n")
        title = terminal_title.conversation_title("s1", str(self.transcript))
        self.assertEqual(title, "AI Generated Title")

    def test_fallback_to_claude_code(self):
        state = {"conversation": "s1", "fallback": "Claude Code"}
        self.assertEqual(terminal_title.title_text(state), "Claude Code")

    def test_fallback_default_antigravity(self):
        state = {"conversation": "s1"}
        self.assertEqual(terminal_title.title_text(state), "Antigravity")

    def test_session_title_precedence(self):
        state = {"name": "Explicit Session Title", "conversation": "s1"}
        self.assertEqual(terminal_title.title_text(state), "Explicit Session Title")


class TestClaudeHookHandler(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_dir = Path(self.tmp_dir.name) / "title_state"
        self.tools_dir = self.state_dir / "tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.patcher1 = patch.object(terminal_title, "STATE_DIR", self.state_dir)
        self.patcher2 = patch.object(tool_state, "STATE_DIR", self.state_dir)
        self.patcher1.start()
        self.patcher2.start()
        self.addCleanup(self.patcher1.stop)
        self.addCleanup(self.patcher2.stop)

    @patch("terminal_title.start_ticker")
    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_session_start(self, mock_find, mock_tty, mock_start):
        payload = {
            "session_id": "sess-1",
            "session_title": "Test Title",
            "transcript_path": "/tmp/test.jsonl",
        }
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            tool_timer.main(["SessionStart"])
        mock_find.assert_called_once_with(comm_names=["claude", ".claude-wrapped"])
        mock_start.assert_called_once_with("/dev/pts/99")
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("conversation"), "sess-1")
        self.assertEqual(state.get("name"), "Test Title")
        self.assertEqual(state.get("working"), False)
        self.assertEqual(state.get("idle_glyph"), "✳")
        self.assertEqual(state.get("working_frames"), ["✶", "✷", "✸", "✹"])
        self.assertEqual(state.get("fallback"), "Claude Code")
        self.assertEqual(state.get("comm_names"), ["claude", ".claude-wrapped"])

    @patch("terminal_title.start_ticker")
    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_tool_timer_writes_custom_cli_args_into_state(self, mock_find, mock_tty, mock_start):
        payload = {"session_id": "sess-custom"}
        cli_args = [
            "SessionStart",
            "--idle-glyph", "✦",
            "--working-glyphs", "1 2 3",
            "--fallback", "Custom Claude",
            "--comm-names", "claude_custom",
        ]
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            tool_timer.main(cli_args)
        mock_find.assert_called_once_with(comm_names=["claude_custom"])
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("idle_glyph"), "✦")
        self.assertEqual(state.get("working_frames"), ["1", "2", "3"])
        self.assertEqual(state.get("fallback"), "Custom Claude")
        self.assertEqual(state.get("comm_names"), ["claude_custom"])

    @patch("terminal_title.start_ticker")
    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_user_prompt_submit(self, mock_find, mock_tty, mock_start):
        payload = {"session_id": "sess-1", "session_title": "Test Title"}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            tool_timer.main(["UserPromptSubmit"])
        mock_start.assert_called_once_with("/dev/pts/99")
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("working"), True)

    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_pre_and_post_tool_use(self, mock_find, mock_tty):
        pre_payload = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_use_id": "call-1",
        }
        with patch("sys.stdin", io.StringIO(json.dumps(pre_payload))):
            tool_timer.main(["PreToolUse"])
        tool_rec = tool_state.load("sess-1")
        self.assertIsNotNone(tool_rec)
        self.assertEqual(tool_rec["tool"], "Bash")
        self.assertEqual(tool_rec["id"], "call-1")

        post_payload = {
            "session_id": "sess-1",
            "tool_use_id": "call-1",
        }
        with patch("sys.stdin", io.StringIO(json.dumps(post_payload))):
            tool_timer.main(["PostToolUse"])
        self.assertIsNone(tool_state.load("sess-1"))

    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_mcp_tool_name_resolution(self, mock_find, mock_tty):
        pre_payload = {
            "session_id": "sess-mcp",
            "tool_name": "call_mcp_tool",
            "tool_input": {"ServerName": "rlm_kernel", "ToolName": "ipython"},
            "tool_use_id": "call-2",
        }
        with patch("sys.stdin", io.StringIO(json.dumps(pre_payload))):
            tool_timer.main(["PreToolUse"])
        tool_rec = tool_state.load("sess-mcp")
        self.assertIsNotNone(tool_rec)
        self.assertEqual(tool_rec["tool"], "rlm_kernel/ipython")

    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_stop_marks_idle(self, mock_find, mock_tty):
        terminal_title.save("/dev/pts/99", {"working": True})
        with patch("sys.stdin", io.StringIO(json.dumps({}))):
            tool_timer.main(["Stop"])
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("working"), False)

    @patch("terminal_title.start_ticker")
    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_state_of_a_previous_tty_owner_is_dropped(self, mock_find, mock_tty, mock_start):
        terminal_title.save("/dev/pts/99", {"agent": 999, "agy": 999, "name": "Old", "comm_names": ["x"]})
        with patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-2"}))):
            tool_timer.main(["SessionStart"])
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("agent"), 12345)
        self.assertIsNone(state.get("name"))
        # agy writes only "agy"; a dead Claude's "agent" key would make the ticker quit at once.
        terminal_title.save("/dev/pts/99", {"agy": 4242, "working": False})
        state = terminal_title.load("/dev/pts/99")
        self.assertNotIn("agent", state)
        self.assertEqual(state.get("agy"), 4242)
        terminal_title.save("/dev/pts/99", {"working": True})
        self.assertEqual(terminal_title.load("/dev/pts/99").get("agy"), 4242)

    @patch("terminal_title.emit_to_tty")
    @patch("terminal_title.tty_path", return_value="/dev/pts/99")
    @patch("terminal_title.find_agent", return_value=12345)
    def test_session_end_clears_title(self, mock_find, mock_tty, mock_emit):
        with patch("sys.stdin", io.StringIO(json.dumps({}))):
            tool_timer.main(["SessionEnd"])
        mock_emit.assert_called_once_with("/dev/pts/99", "")
        state = terminal_title.load("/dev/pts/99")
        self.assertEqual(state.get("working"), False)


class TestAgyIgnoresClaudeEnv(unittest.TestCase):
    def test_agy_ignores_agent_env_vars(self):
        claude_env = {
            "AGENT_TITLE_IDLE_GLYPH": "✳",
            "AGENT_TITLE_WORKING_GLYPHS": "✶ ✷ ✸ ✹",
            "AGENT_TITLE_FALLBACK": "Claude Code",
            "AGENT_PROCESS_NAMES": "claude .claude-wrapped",
        }
        with patch.dict(os.environ, claude_env):
            self.assertEqual(terminal_title.IDLE_GLYPH, "△")
            self.assertEqual(terminal_title.WORKING_FRAMES, ["▲", "▶", "▼", "◀"])
            self.assertEqual(terminal_title.FALLBACK, "Antigravity")
            self.assertEqual(terminal_title.DEFAULT_PROCESS_NAMES, ["agy"])

            agy_state = {"conversation": "agy-sess"}
            self.assertEqual(terminal_title.title_text(agy_state), "Antigravity")


class TestTitleComposition(unittest.TestCase):
    def test_compose_idle_title(self):
        title = terminal_title.compose_title("✳", "Claude Code")
        self.assertEqual(title, "✳ Claude Code")

    def test_compose_working_title_with_tool(self):
        title = terminal_title.compose_title("✶", "Claude Code", "⏱ Bash 5s")
        self.assertEqual(title, "✶ Claude Code · ⏱ Bash 5s")


if __name__ == "__main__":
    unittest.main()
