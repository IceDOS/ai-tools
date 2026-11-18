import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import statusline
import terminal_title


class TestStatuslineQuota(unittest.TestCase):
    def test_merge_converts_reset_ts_dynamically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_file = tmp_path / "quota_cache.json"
            base_time = 1_800_000_000.0
            reset_ts = base_time + 3600.0
            cache_file.write_text(json.dumps({
                "ts": base_time,
                "quota": {
                    "gemini-5h": {
                        "remaining_fraction": 0.5,
                        "reset_time": "2027-01-15T09:00:00Z",
                        "reset_ts": reset_ts,
                        "reset_in_seconds": 3600.0,
                    }
                }
            }))
            payload = {}
            with mock.patch.object(statusline, "QUOTA_CACHE", cache_file), \
                 mock.patch("time.time", return_value=base_time + 40.0):
                statusline.maybe_refresh_quota(payload)

            q = payload["quota"]["gemini-5h"]
            self.assertEqual(q["remaining_fraction"], 0.5)
            self.assertEqual(q["reset_time"], "2027-01-15T09:00:00Z")
            self.assertAlmostEqual(q["reset_in_seconds"], 3560.0)

    def test_fetch_quota_records_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_file = tmp_path / "quota_cache.json"
            lock_file = tmp_path / "quota_fetch.lock"
            fake_quota_out = json.dumps({
                "command": {
                    "data": {
                        "groups": [{
                            "buckets": [{
                                "id": "gemini-5h",
                                "remaining_fraction": 0.75,
                                "reset_time": "2027-01-15T09:00:00Z",
                            }]
                        }]
                    }
                }
            })
            fake_proc = mock.Mock(returncode=0, stdout=fake_quota_out)
            recorded = []

            def fake_run(cmd, *args, **kwargs):
                if cmd[0] == "mock_agy":
                    return fake_proc
                if len(cmd) >= 2 and cmd[1] == "record":
                    recorded.append(json.loads(kwargs.get("input", "{}")))
                    return mock.Mock(returncode=0)
                return mock.Mock(returncode=0)

            with mock.patch.object(statusline, "CACHE_DIR", tmp_path), \
                 mock.patch.object(statusline, "QUOTA_CACHE", cache_file), \
                 mock.patch.object(statusline, "QUOTA_LOCK", lock_file), \
                 mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch("time.time", return_value=1_800_000_000.0):
                code = statusline.fetch_quota(agy_bin="mock_agy")
                self.assertEqual(code, 0)

            cached = json.loads(cache_file.read_text())
            bucket = cached["quota"]["gemini-5h"]
            self.assertEqual(bucket["remaining_fraction"], 0.75)
            self.assertEqual(bucket["reset_time"], "2027-01-15T09:00:00Z")
            self.assertIn("reset_ts", bucket)
            self.assertTrue(len(recorded) > 0)



class FakeTime:
    """Stands in for terminal_title's time module, so ticks do not sleep."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.1
        return self.now

    def sleep(self, seconds):
        pass


class TestTerminalTitle(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state_dir = self.dir / "state"
        tty = self.dir / "tty"
        tty.touch()
        self.tty = str(tty)
        self.patch("STATE_DIR", self.state_dir)

    def patch(self, name, value):
        patcher = mock.patch(f"terminal_title.{name}", value)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tick(self, state, alive=3):
        """Run one ticker to its end; return the titles it drew."""
        terminal_title.save(self.tty, state)
        titles = mock.Mock()
        self.patch("time", FakeTime())
        self.patch("emit", titles)
        self.patch("proc_stat", mock.Mock(side_effect=[("agy", 1)] * alive + [None] * 5))
        terminal_title.tick(self.tty)
        return [call.args[1] for call in titles.call_args_list]

    def test_title_text_prefers_the_name_the_user_gave(self):
        generated = self.patch("conversation_title", mock.Mock(return_value="Generated"))
        self.assertEqual(terminal_title.title_text({"name": "My task", "conversation": "c1"}), "My task")
        generated.assert_not_called()

    def test_title_text_falls_back_to_the_generated_title(self):
        self.patch("conversation_title", mock.Mock(return_value="Generated title"))
        self.assertEqual(terminal_title.title_text({"conversation": "c1"}), "Generated title")

    def test_title_text_falls_back_to_the_agent_name(self):
        self.patch("conversation_title", mock.Mock(return_value=""))
        self.assertEqual(terminal_title.title_text({}), "Antigravity")

    def test_title_text_is_cut_to_the_title_width(self):
        text = terminal_title.title_text({"name": "x" * 200})
        self.assertEqual(len(text), terminal_title.TITLE_MAX)
        self.assertTrue(text.endswith("\u2026"))

    def test_conversation_title_reads_the_generated_title_then_the_first_prompt(self):
        db = self.dir / "summaries.db"
        connection = sqlite3.connect(db)
        connection.execute("create table conversation_summaries (conversation_id, title, preview)")
        connection.executemany(
            "insert into conversation_summaries values (?, ?, ?)",
            [("c1", "Generated title", "first prompt"), ("c2", "", "first prompt"), ("c3", "", "")],
        )
        connection.commit()
        connection.close()
        self.patch("SUMMARIES", db)
        self.assertEqual(terminal_title.conversation_title("c1"), "Generated title")
        self.assertEqual(terminal_title.conversation_title("c2"), "first prompt")
        self.assertEqual(terminal_title.conversation_title("c3"), "")
        self.assertEqual(terminal_title.conversation_title(None), "")

    def test_emit_writes_an_osc_zero_sequence(self):
        path = self.dir / "osc"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOCTTY)
        terminal_title.emit(fd, "\u25b2 Antigravity")
        os.close(fd)
        self.assertEqual(path.read_bytes(), "\033]0;\u25b2 Antigravity\007".encode())

    def test_tick_cycles_the_working_glyphs(self):
        titles = self.tick({"agy": 7, "name": "Task", "working": True})
        self.assertEqual(titles, ["\u2596 Task", "\u2598 Task", "\u259d Task", ""])

    def test_tick_shows_the_idle_glyph_once_and_clears_on_exit(self):
        titles = self.tick({"agy": 7, "name": "Task", "working": False})
        self.assertEqual(titles, ["\u25b2 Task", ""])

    def test_tick_gives_up_when_another_ticker_holds_the_lock(self):
        terminal_title.save(self.tty, {"agy": 7, "name": "Task", "working": True})
        titles = self.patch("emit", mock.Mock())
        os.close(terminal_title.ticker_lock(self.tty))  # released: tick may run
        lock = terminal_title.ticker_lock(self.tty)
        self.addCleanup(os.close, lock)
        terminal_title.tick(self.tty)
        titles.assert_not_called()

    def test_update_saves_the_state_and_starts_one_ticker(self):
        self.patch("find_agy", mock.Mock(return_value=7))
        self.patch("tty_path", mock.Mock(return_value=self.tty))
        popen = self.patch("subprocess.Popen", mock.Mock())
        terminal_title.update(
            {"agent_state": "working", "conversation_id": "c1", "conversation_title": "My task"}
        )
        self.assertEqual(
            terminal_title.load(self.tty),
            {"agy": 7, "conversation": "c1", "name": "My task", "working": True},
        )
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0][2:], ["tick", self.tty])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

        popen.reset_mock()
        lock = terminal_title.ticker_lock(self.tty)  # a ticker already runs
        self.addCleanup(os.close, lock)
        terminal_title.update({"agent_state": "idle"})
        popen.assert_not_called()

    def test_update_ignores_a_status_line_outside_agy(self):
        self.patch("find_agy", mock.Mock(return_value=None))
        popen = self.patch("subprocess.Popen", mock.Mock())
        terminal_title.update({"agent_state": "working"})
        popen.assert_not_called()
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
