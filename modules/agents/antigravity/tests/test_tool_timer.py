import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tool_state
import tool_timer

BASE = 1_800_000_000.0


class TestToolTimerHook(unittest.TestCase):
    """The hook agy runs around every tool call: it answers JSON and records the call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        for patcher in (
            mock.patch("tool_state.STATE_DIR", self.state_dir),
            mock.patch("time.time", mock.Mock(return_value=BASE)),
        ):
            self.addCleanup(patcher.stop)
            patcher.start()

    def run_hook(self, event, payload):
        """Run the hook as agy does: the event in argv, the payload on stdin."""
        out = io.StringIO()
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        argv = [event] if event is not None else []
        with mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(out):
            code = tool_timer.main(argv)
        return code, out.getvalue()

    def start(self, call_id="toolu_01", name="run_command", conversation="c1", step=19):
        return self.run_hook(
            "PreToolUse",
            {
                "hookEventName": "PreToolUse",
                "conversationId": conversation,
                "stepIdx": step,
                "toolCall": {"id": call_id, "name": name, "args": {"CommandLine": "sleep 20"}},
            },
        )

    def test_pre_tool_use_records_the_call_and_asks(self):
        code, out = self.start()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"decision": "ask"})
        self.assertEqual(
            tool_state.load("c1"),
            {"tool": "run_command", "start_ts": BASE, "id": "toolu_01", "step": 19},
        )

    def test_an_mcp_call_is_named_server_slash_tool(self):
        args = {"ServerName": '"rlm_kernel"', "ToolName": '"ipython"', "Arguments": '{"code": "1"}'}
        self.run_hook("PreToolUse", {"conversationId": "c1", "toolCall": {"name": "call_mcp_tool", "args": args}})
        self.assertEqual(tool_state.load("c1")["tool"], "rlm_kernel/ipython")
        self.run_hook("PreToolUse", {"conversationId": "c2", "toolCall": {"name": "call_mcp_tool", "args": {}}})
        self.assertEqual(tool_state.load("c2")["tool"], "call_mcp_tool")

    def test_the_event_also_comes_from_the_payload(self):
        code, _ = self.run_hook(
            None,
            {"hookEventName": "PreToolUse", "conversationId": "c1", "toolCall": {"name": "view_file"}},
        )
        self.assertEqual(code, 0)
        self.assertEqual(tool_state.load("c1")["tool"], "view_file")

    def test_post_tool_use_clears_the_call(self):
        self.start()
        self.run_hook("PostToolUse", {"conversationId": "c1", "toolCall": {"id": "toolu_01"}})
        self.assertIsNone(tool_state.load("c1"))

    def test_post_tool_use_keeps_a_newer_call(self):
        self.start(call_id="toolu_02")
        self.run_hook("PostToolUse", {"conversationId": "c1", "toolCall": {"id": "toolu_01"}})
        self.assertIsNotNone(tool_state.load("c1"))

    def test_stop_keeps_the_call(self):
        """Stop fires while an async tool still runs, so it must not clear the record."""
        self.start()
        self.run_hook("Stop", {"conversationId": "c1"})
        self.assertIsNotNone(tool_state.load("c1"))

    def test_a_payload_that_is_not_json_still_answers(self):
        code, out = self.run_hook("PreToolUse", "not json")
        self.assertEqual((code, json.loads(out)), (0, {"decision": "ask"}))
        self.assertFalse(self.state_dir.joinpath("tools").exists())

    def test_the_model_call_events_keep_the_call(self):
        """The invocation events fire while an async tool still runs, so only PostToolUse clears."""
        for event in ("PreInvocation", "PostInvocation"):
            with self.subTest(event=event):
                self.start()
                self.run_hook(event, {"hookEventName": event, "conversationId": "c1"})
                self.assertIsNotNone(tool_state.load("c1"))

    def test_an_unknown_event_records_nothing(self):
        self.start()
        self.run_hook("SessionStart", {"conversationId": "c2"})
        self.assertIsNone(tool_state.load("c2"))
        self.assertIsNotNone(tool_state.load("c1"))

    def test_a_call_without_a_conversation_records_nothing(self):
        self.run_hook("PreToolUse", {"toolCall": {"name": "run_command"}})
        self.assertFalse(self.state_dir.joinpath("tools").exists())

    def test_the_safety_net_outlasts_the_longest_tool_timeout(self):
        self.assertGreater(tool_state.MAX_AGE, 600)
        tool_state.save("c1", "run_command", started=BASE - 599)
        self.assertIsNotNone(tool_state.load("c1"))


if __name__ == "__main__":
    unittest.main()
