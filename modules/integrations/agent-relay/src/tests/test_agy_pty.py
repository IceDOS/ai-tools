import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
import agy_pty  # noqa: E402

SCRIPT = os.path.join(SRC, "agy_pty.py")

# Stand-in for agy: optionally enables bracketed paste, then records one submitted prompt.
FAKE_AGY = r"""
import os, sys
if sys.argv[2] == "1":
    os.write(1, b"\x1b[?2004h")
buf = b""
while not (buf.endswith(b"\n") and (sys.argv[2] == "0" or b"\x1b[201~" in buf)):
    buf += os.read(0, 4096)
open(sys.argv[1], "wb").write(buf)
"""

# Stand-in for a busy agy: after the first input byte it writes 2MB before reading the rest of the paste.
FLOOD_AGY = r"""
import os, sys, tty
tty.setraw(0)
os.write(1, b"\x1b[?2004h")
buf = os.read(0, 1)
out = b"x" * 2_000_000
while out:
    out = out[os.write(1, out):]
while not buf.endswith(b"\r"):
    buf += os.read(0, 65536)
open(sys.argv[1], "wb").write(buf)
"""


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if v := fn():
            return v
        time.sleep(0.05)
    raise TimeoutError


def request(path, obj):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall((json.dumps(obj) + "\n").encode())
        return s.makefile().readline().strip()


class InputStateTest(unittest.TestCase):
    def test_draft_tracking(self):
        s = agy_pty.InputState()
        self.assertFalse(s.dirty)
        s.user(b"hel")
        self.assertTrue(s.dirty)
        s.user(b"\x1b[D")
        self.assertTrue(s.dirty)
        s.user(b"\r")
        self.assertFalse(s.dirty)
        s.user(b"x")
        s.user(b"\x15")
        self.assertFalse(s.dirty)
        s.user(b"y\x03")
        self.assertFalse(s.dirty)

    def test_backspace_and_a_bare_esc_clear_the_draft(self):
        s = agy_pty.InputState()
        s.user(b"x")
        self.assertTrue(s.dirty)
        s.user(b"\x7f")
        self.assertFalse(s.dirty)
        s.user(b"x")
        s.user(b"\x1b")
        self.assertFalse(s.dirty)

    def test_backspace_never_counts_below_zero(self):
        s = agy_pty.InputState()
        s.user(b"\x7f\x7f\x7f")
        self.assertFalse(s.dirty)
        s.user(b"a")
        self.assertTrue(s.dirty)

    def test_multibyte_input_counts_once(self):
        s = agy_pty.InputState()
        s.user("é".encode())
        self.assertEqual(s.draft, 1)
        s.user("\U0001f600".encode())
        self.assertEqual(s.draft, 2)
        s.user(b"\x7f\x7f")
        self.assertFalse(s.dirty)

    def test_idle_follows_agy_output(self):
        s = agy_pty.InputState()
        self.assertFalse(s.idle())
        s.last_output -= agy_pty.IDLE_SECONDS + 0.1
        self.assertTrue(s.idle())
        s.agy(b"tick")
        self.assertFalse(s.idle())

    def test_silent_ms_reports_the_gap_since_the_last_agy_output(self):
        # The watcher needs the real gap, not the 2s idle flag, to tell an orphaned turn from a slow one.
        s = agy_pty.InputState()
        s.last_output -= 1.5
        self.assertGreaterEqual(s.silent_ms(), 1400)
        s.agy(b"tick")
        self.assertLess(s.silent_ms(), 100)

    def test_mode_toggles_alone_keep_agy_silent(self):
        s = agy_pty.InputState()
        s.last_output -= 5
        s.agy(b"\x1b[?2004h\x1b[>4;2m")
        self.assertGreaterEqual(s.silent_ms(), 4900)
        self.assertTrue(s.bracketed)
        s.agy(b"\x1b[?2004htick")
        self.assertLess(s.silent_ms(), 100)

    def test_bracketed_mode_follows_agy_output_split_across_reads(self):
        s = agy_pty.InputState()
        s.agy(b"hello \x1b[?20")
        s.agy(b"04h world")
        self.assertTrue(s.bracketed)
        s.agy(b"\x1b[?2004l")
        self.assertFalse(s.bracketed)

    def test_encode_paste(self):
        self.assertEqual(agy_pty.encode_paste("a\nb", True), b"\x1b[200~a\nb\x1b[201~")
        self.assertEqual(agy_pty.encode_paste("a\n  b\n", False), b"a b")

    def test_encode_paste_strips_control_bytes_that_could_escape_the_paste(self):
        hostile = "a\x1b[201~\rrm -rf x\x07\x9b\tb"
        self.assertEqual(agy_pty.encode_paste(hostile, True), b"\x1b[200~a[201~rm -rf x\tb\x1b[201~")
        self.assertEqual(agy_pty.encode_paste("é\n—", True), "\x1b[200~é\n—\x1b[201~".encode())

    def test_a_bracketed_paste_counts_as_draft(self):
        s = agy_pty.InputState()
        s.user(agy_pty.PASTE_START + b"pasted\nby hand" + agy_pty.PASTE_END)
        self.assertTrue(s.dirty)
        s.user(b"\r")
        self.assertFalse(s.dirty)


class ProxyTest(unittest.TestCase):
    def start(self, d, bracketed, agy=FAKE_AGY):
        out = os.path.join(d, "out")
        env = {**os.environ, "XDG_RUNTIME_DIR": d}
        env.pop("AGENT_RELAY_AGY_WATCH", None)
        p = subprocess.Popen(
            [sys.executable, SCRIPT, sys.executable, "-c", agy, out, "1" if bracketed else "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.stdin.close(), p.wait()))
        sock = wait_for(lambda: glob.glob(os.path.join(d, "agent-relay", "pty", "*.sock")))[0]
        time.sleep(0.3)  # let the fake agy announce bracketed paste
        return p, out, sock

    def test_bracketed_paste_reaches_agy_and_submits(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, sock = self.start(d, True)
            self.assertEqual(request(sock, {"paste": "[relay from pi a, hop 1]\nhi"}), "ok")
            self.assertEqual(p.wait(timeout=10), 0)
            with open(out, "rb") as f:
                data = f.read()
            self.assertIn(b"\x1b[200~[relay from pi a, hop 1]\nhi\x1b[201~", data)
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(glob.glob(os.path.join(d, "agent-relay", "pty", "*")), [])

    def test_plain_paste_collapses_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, sock = self.start(d, False)
            self.assertEqual(request(sock, {"paste": "a\nb"}), "ok")
            p.wait(timeout=10)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"a b\n")

    def test_status_answers_idle_and_dirty(self):
        with tempfile.TemporaryDirectory() as d:
            p, _, sock = self.start(d, True)
            p.stdin.write(b"x")
            p.stdin.flush()
            self.assertTrue(wait_for(lambda: json.loads(request(sock, {"status": True}))["dirty"]))
            self.assertTrue(wait_for(lambda: json.loads(request(sock, {"status": True}))["idle"], timeout=6))
            self.assertIn("silentMs", json.loads(request(sock, {"status": True})))

    def test_draft_blocks_paste(self):
        with tempfile.TemporaryDirectory() as d:
            p, _, sock = self.start(d, True)
            p.stdin.write(b"x")
            p.stdin.flush()
            time.sleep(0.2)
            self.assertEqual(request(sock, {"paste": "hi"}), "dirty")
            self.assertEqual(request(sock, {"nope": 1}), "error")

    def test_a_large_paste_into_a_flooding_agy_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, sock = self.start(d, True, FLOOD_AGY)
            text = "y" * 300_000
            self.assertEqual(request(sock, {"paste": text}), "ok")
            self.assertEqual(p.wait(timeout=10), 0)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"\x1b[200~" + text.encode() + b"\x1b[201~\r")


if __name__ == "__main__":
    unittest.main()
