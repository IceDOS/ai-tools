"""The rlm() children the title counts and the bar shows, from the plugin's child logs."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import statusline
import terminal_title
import tool_state

CONV = "1e563473-2a6a-4f92-ae44-cfd7ea33db78"


def start(child_id, parent=None, depth=1, pid=None):
    record = {
        "type": "rlm_child_start", "id": child_id, "parent": parent, "depth": depth,
        "model": None, "ts": 1.0, "prompt": "task",
    }
    if pid:  # the rlm server's pid; older logs have none
        record["pid"] = pid
    return json.dumps(record)


def end(child_id, ok=True):
    return json.dumps({
        "type": "rlm_child_end", "id": child_id, "ok": ok, "status": "SUCCESS",
        "usage": {}, "turns": 1, "ts": 2.0,
    })


def dead_pid():
    """A pid that certainly does not run: a child that already exited and was reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def zombie_pid():
    """A pid whose /proc entry still exists with state Z, until the test reaps it."""
    if not hasattr(os, "fork"):
        raise unittest.SkipTest("needs fork and /proc")
    pid = os.fork()
    if pid == 0:  # child: exit without the parent reaping it, which leaves a zombie
        os._exit(0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        if stat[stat.rindex(")") + 2 :].split()[0] == "Z":
            break
        time.sleep(0.005)
    return pid


def reap(pid):
    """Collect the test's zombie; it is reaped only once the checks are done."""
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class TestChildLogs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data_dir = self.dir / "rlm"
        self.logs = tool_state.children_dir(self.data_dir, CONV)

    def log(self, child_id, *records):
        self.logs.mkdir(parents=True, exist_ok=True)
        (self.logs / f"{child_id}.jsonl").write_text("".join(r + "\n" for r in records))

    def test_children_dir_is_the_conversation_dir(self):
        self.assertEqual(tool_state.children_dir(self.data_dir, CONV), self.data_dir / "children" / CONV)
        self.assertEqual(tool_state.children_dir(self.data_dir, "a/b"), self.data_dir / "children" / "a_b")
        self.assertIsNone(tool_state.children_dir(self.data_dir, None))
        self.assertIsNone(tool_state.children_dir(None, CONV))

    def test_a_child_runs_until_its_end_record(self):
        self.log("running", start("running"))
        self.log("done", start("done"), end("done"))
        self.assertTrue(tool_state.child_running(self.logs / "running.jsonl"))
        self.assertFalse(tool_state.child_running(self.logs / "done.jsonl"))
        self.assertFalse(tool_state.child_running(self.logs / "missing.jsonl"))

    def test_a_long_log_costs_only_its_tail(self):
        """A 300 KB stream with the end record last is decided from the last KiB."""
        middle = json.dumps({"type": "message", "text": "x" * 300_000})
        self.log("big", start("big"), middle, end("big"))
        self.assertGreater((self.logs / "big.jsonl").stat().st_size, 300_000)
        self.assertFalse(tool_state.child_running(self.logs / "big.jsonl"))
        self.assertLessEqual(tool_state.CHILD_TAIL, 2048)

    def test_a_half_written_line_still_counts_as_running(self):
        path = self.logs / "partial.jsonl"
        self.log("partial", start("partial"))
        with open(path, "a") as f:
            f.write('{"type": "message", "te')
        self.assertTrue(tool_state.child_running(path))

    def test_a_child_whose_server_died_counts_as_ended(self):
        """A killed server or an interrupted cell never writes the end record."""
        pid = dead_pid()
        self.log("orphan", start("orphan", pid=pid))
        self.assertFalse(tool_state.child_running(self.logs / "orphan.jsonl", pid))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 0)

    def test_a_child_whose_server_lives_still_runs(self):
        self.log("live", start("live", pid=os.getpid()))
        self.assertTrue(tool_state.child_running(self.logs / "live.jsonl", os.getpid()))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 1)

    def test_a_zombie_server_counts_as_ended(self):
        pid = zombie_pid()
        self.addCleanup(reap, pid)
        self.log("zombie", start("zombie", pid=pid))
        self.assertFalse(tool_state.pid_alive(pid))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 0)

    def test_a_child_started_without_a_pid_keeps_the_record_rule(self):
        self.log("old", start("old"))
        self.assertTrue(tool_state.child_running(self.logs / "old.jsonl"))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 1)

    def test_the_start_record_is_read_once_per_log(self):
        self.log("a", start("a", pid=os.getpid()))
        clock = Clock()
        counter = tool_state.ChildrenCounter(self.data_dir, clock=clock)
        with mock.patch.object(tool_state, "child_start", wraps=tool_state.child_start) as read:
            for _ in range(3):
                counter.count(CONV)
                clock.now += tool_state.CHILD_SCAN
        self.assertEqual(read.call_count, 1)

    def test_an_end_record_in_another_key_order_ends_the_child(self):
        """json.dumps keeps insertion order, so the record is parsed, not matched bytewise."""
        reordered = json.dumps({"ok": True, "type": "rlm_child_end", "id": "a"}, separators=(", ", ": "))
        self.assertNotIn('{"type": "rlm_child_end"', reordered)
        self.log("a", start("a"), reordered)
        self.assertFalse(tool_state.child_running(self.logs / "a.jsonl"))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 0)

    def test_a_half_written_end_record_keeps_the_child_running(self):
        path = self.logs / "a.jsonl"
        self.log("a", start("a"), end("a")[:-5])
        self.assertTrue(tool_state.child_running(path))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 1)

    def test_counts_the_children_without_an_end_record(self):
        self.log("a", start("a"))
        self.log("b", start("b"))
        self.log("c", start("c"), end("c"))
        self.log("d", start("d", parent="a", depth=2))
        counter = tool_state.ChildrenCounter(self.data_dir)
        self.assertEqual(counter.count(CONV), 3)
        self.assertEqual(counter.by_depth(CONV), {1: 2, 2: 1})

    def test_nothing_to_count_is_zero(self):
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(CONV), 0)
        self.assertEqual(tool_state.ChildrenCounter(None).count(CONV), 0)
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count(None), 0)

    def test_a_scan_runs_once_per_interval(self):
        self.log("a", start("a"))
        clock = Clock()
        counter = tool_state.ChildrenCounter(self.data_dir, clock=clock)
        self.assertEqual(counter.count(CONV), 1)
        self.log("b", start("b"))
        self.assertEqual(counter.count(CONV), 1)
        clock.now += tool_state.CHILD_SCAN
        self.assertEqual(counter.count(CONV), 2)

    def test_an_ended_log_is_never_read_again(self):
        self.log("a", start("a"), end("a"))
        clock = Clock()
        counter = tool_state.ChildrenCounter(self.data_dir, clock=clock)
        with mock.patch.object(tool_state, "child_running", wraps=tool_state.child_running) as read:
            for _ in range(3):
                self.assertEqual(counter.count(CONV), 0)
                clock.now += tool_state.CHILD_SCAN
        self.assertEqual(read.call_count, 1)

    def test_the_rlm_session_names_the_conversation_when_the_bar_id_differs(self):
        """The rlm server logs under the session's conversationId; the bar may report another."""
        self.log("a", start("a"))
        sessions = self.data_dir / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps({"conversationId": CONV}))
        self.assertEqual(tool_state.session_conversation(self.data_dir, 4242), CONV)
        self.assertIsNone(tool_state.session_conversation(self.data_dir, 1))
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count("other-id", 4242), 1)
        self.assertEqual(tool_state.ChildrenCounter(self.data_dir).count("other-id"), 0)

    def test_the_data_dir_comes_from_the_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tool_state.rlm_data_dir())
        with mock.patch.dict(os.environ, {tool_state.RLM_DATA_ENV: str(self.data_dir)}):
            self.assertEqual(tool_state.rlm_data_dir(), self.data_dir)


class TestTitleChildren(unittest.TestCase):
    """The ticker is the only surface agy does not freeze during a tool call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data_dir = self.dir / "rlm"
        logs = tool_state.children_dir(self.data_dir, CONV)
        logs.mkdir(parents=True)
        (logs / "a.jsonl").write_text(start("a") + "\n")
        (logs / "b.jsonl").write_text(start("b") + "\n" + end("b") + "\n")
        tty = self.dir / "tty"
        tty.touch()
        self.tty = str(tty)
        for patcher in (
            mock.patch.object(terminal_title, "STATE_DIR", self.dir / "state"),
            mock.patch.object(tool_state, "STATE_DIR", self.dir / "state"),
            mock.patch.dict(os.environ, {tool_state.RLM_DATA_ENV: str(self.data_dir)}),
        ):
            self.addCleanup(patcher.stop)
            patcher.start()

    def test_the_title_counts_the_running_children(self):
        titles = tick_once(self.tty, {"agy": 7, "name": "Task", "conversation": CONV, "working": True})
        self.assertEqual(titles, ["\u2596 Task \u00b7 1 rlm d1:1", "\u2598 Task \u00b7 1 rlm d1:1", "\u259d Task \u00b7 1 rlm d1:1", ""])

    def test_the_title_keeps_the_tool_timer_and_appends_the_children(self):
        with mock.patch.object(tool_state, "time", FakeTime(100.0)):
            tool_state.save(CONV, "mcp__kernel__ipython", started=92.0)  # 8s, on both clocks
            titles = tick_once(self.tty, {"agy": 7, "name": "Task", "conversation": CONV, "working": False})
        self.assertEqual(titles[0], "\u2596 Task \u00b7 \u23f1 ipython 8s \u00b7 1 rlm d1:1")

    def test_no_children_leaves_the_title_alone(self):
        (tool_state.children_dir(self.data_dir, CONV) / "a.jsonl").write_text(start("a") + "\n" + end("a") + "\n")
        titles = tick_once(self.tty, {"agy": 7, "name": "Task", "conversation": CONV, "working": False})
        self.assertEqual(titles, ["\u25b2 Task", ""])

    def test_the_count_is_off_without_the_plugin(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            titles = tick_once(self.tty, {"agy": 7, "name": "Task", "conversation": CONV, "working": False})
        self.assertEqual(titles, ["\u25b2 Task", ""])


class FakeTime:
    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        self.now += 0.1
        return self.now

    def sleep(self, seconds):
        pass

    def time(self):
        return self.now


def tick_once(tty, state, alive=3, start=100.0):
    """Run one ticker over a fake terminal to its end; return the titles it drew."""
    with mock.patch.object(terminal_title, "time", FakeTime(start)), \
         mock.patch.object(terminal_title, "emit", mock.Mock()) as emit, \
         mock.patch.object(terminal_title, "proc_stat", mock.Mock(side_effect=[("agy", 1)] * alive + [None] * 5)):
        terminal_title.save(tty, state)
        terminal_title.tick(tty)
    return [call.args[1] for call in emit.call_args_list]


class TestStatuslineSegment(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data_dir = self.dir / "rlm"
        logs = tool_state.children_dir(self.data_dir, CONV)
        logs.mkdir(parents=True)
        (logs / "a.jsonl").write_text(start("a") + "\n")
        (logs / "b.jsonl").write_text(start("b") + "\n")
        (logs / "c.jsonl").write_text(start("c") + "\n" + end("c") + "\n")
        patcher = mock.patch.dict(os.environ, {tool_state.RLM_DATA_ENV: str(self.data_dir)})
        self.addCleanup(patcher.stop)
        patcher.start()

    def run_main(self, bar="HEAD  \u2502 5h 31%\nBAR", payload=None, argv=()):
        out = io.StringIO()
        with mock.patch.object(statusline, "read_payload", return_value=payload or {"conversation_id": CONV}), \
             mock.patch.object(statusline, "maybe_refresh_quota"), \
             mock.patch.object(statusline, "render", return_value=bar), \
             mock.patch.object(terminal_title, "find_agy", return_value=7), \
             mock.patch.object(terminal_title, "update", return_value=None), \
             mock.patch("sys.stdout", out):
            statusline.main(list(argv))
        return out.getvalue()

    def test_the_segment_rides_on_the_head_line(self):
        self.assertEqual(self.run_main(), "HEAD  \u2502 5h 31%  rlm 2 running\nBAR\n")

    def test_the_segment_is_absent_without_running_children(self):
        for child in ("a", "b"):
            (tool_state.children_dir(self.data_dir, CONV) / f"{child}.jsonl").write_text(end(child) + "\n")
        self.assertEqual(self.run_main(), "HEAD  \u2502 5h 31%\nBAR\n")

    def test_the_segment_is_absent_with_the_plugin_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.run_main(), "HEAD  \u2502 5h 31%\nBAR\n")

    def test_no_bar_shows_no_segment(self):
        self.assertEqual(self.run_main(argv=["--no-bar"]), "")
        self.assertEqual(self.run_main(bar=""), "")

    def test_a_bar_that_already_ended_flips_nothing(self):
        self.assertEqual(statusline.with_segment("", "rlm 2 running"), "")
        self.assertEqual(statusline.with_segment("BAR", ""), "BAR")


if __name__ == "__main__":
    unittest.main()
