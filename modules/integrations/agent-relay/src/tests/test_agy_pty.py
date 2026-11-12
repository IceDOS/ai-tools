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


class ProxyTest(unittest.TestCase):
    def start(self, d, bracketed):
        out = os.path.join(d, "out")
        env = {**os.environ, "XDG_RUNTIME_DIR": d}
        env.pop("AGENT_RELAY_AGY_WATCH", None)
        p = subprocess.Popen(
            [sys.executable, SCRIPT, sys.executable, "-c", FAKE_AGY, out, "1" if bracketed else "0"],
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
            data = open(out, "rb").read()
            self.assertIn(b"\x1b[200~[relay from pi a, hop 1]\nhi\x1b[201~", data)
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(glob.glob(os.path.join(d, "agent-relay", "pty", "*")), [])

    def test_plain_paste_collapses_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, sock = self.start(d, False)
            self.assertEqual(request(sock, {"paste": "a\nb"}), "ok")
            p.wait(timeout=10)
            self.assertEqual(open(out, "rb").read(), b"a b\n")

    def test_draft_blocks_paste(self):
        with tempfile.TemporaryDirectory() as d:
            p, _, sock = self.start(d, True)
            p.stdin.write(b"x")
            p.stdin.flush()
            time.sleep(0.2)
            self.assertEqual(request(sock, {"paste": "hi"}), "dirty")
            self.assertEqual(request(sock, {"nope": 1}), "error")


if __name__ == "__main__":
    unittest.main()
