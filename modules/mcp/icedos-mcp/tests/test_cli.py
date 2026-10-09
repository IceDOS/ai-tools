"""Tests for the subprocess wrappers in icedos_mcp.cli.

Drives run_icedos/run_icedos_streaming against a fake `icedos` binary so the
timeout paths can be exercised without a real IceDOS install or config.

Run from the repo root with a Python that has tomlkit on the path:

    PYTHONPATH=src python -m unittest tests.test_cli -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from icedos_mcp import cli


def _is_proc_alive(pid: int) -> bool:
    """True only if pid is a live process — a zombie counts as dead.

    os.kill(pid, 0) succeeds for zombies, so read the state field from
    /proc/<pid>/stat ('Z') and treat it as dead rather than relying on
    ProcessLookupError alone.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # field 3 is the state char; comm (field 2) may contain spaces (and even a
    # literal ") "), so rsplit on the closing ')' of the comm field.
    return stat.rsplit(") ", 1)[-1].split()[0] != "Z"


def _make_fake_icedos(root: Path, body: str) -> Path:
    """Create a fake `icedos` executable in root/bin and return its path."""
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "icedos"
    exe.write_text(f"#!{shutil.which('bash')}\n{body}\n")
    exe.chmod(0o755)
    return exe


class _FakeSetupMixin:
    """Build a fake config root (flake.nix) + fake icedos binary per test."""

    body = "echo some-output\nsleep 60\n"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "flake.nix").write_text("{}\n")
        self._pidfile = root / "child.pid"
        self._exe = _make_fake_icedos(root, self.body.format(pidfile=self._pidfile))
        self._old_env = {
            "ICEDOS_CONFIG_DIR": os.environ.get("ICEDOS_CONFIG_DIR"),
            "PATH": os.environ.get("PATH"),
        }
        os.environ["ICEDOS_CONFIG_DIR"] = str(root)
        os.environ["PATH"] = f"{self._exe.parent}{os.pathsep}{os.environ.get('PATH', '')}"

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class StreamingTimeoutTest(_FakeSetupMixin, unittest.TestCase):
    def test_streaming_timeout_raises_and_sets_partial_output(self) -> None:
        with self.assertRaises(TimeoutError) as cm:
            asyncio.run(cli.run_icedos_streaming("gc", timeout=1))
        self.assertIn("timed out after 1s", str(cm.exception))
        partial = getattr(cm.exception, "partial_output", None)
        assert partial is not None  # narrows for the type checker
        self.assertIn("some-output", partial)


@unittest.skipUnless(Path("/proc").is_dir(), "requires /proc (Linux)")
class StreamingKillTest(_FakeSetupMixin, unittest.TestCase):
    body = (
        "echo some-output\n"
        "sleep 60 &\n"
        "echo $! > {pidfile}\n"
        "wait\n"
    )

    def test_streaming_timeout_kills_descendants(self) -> None:
        # the fake icedos spawns a child whose pid is written to a file, so we
        # can confirm the whole process group was killed, not just the leader.
        with self.assertRaises(TimeoutError):
            asyncio.run(cli.run_icedos_streaming("gc", timeout=1))
        child_pid = int(self._pidfile.read_text().strip())
        # killpg(SIGKILL) should have killed the sleep child; poll for the reap.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _is_proc_alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(_is_proc_alive(child_pid), f"child {child_pid} still alive after group kill")


class BufferedTimeoutTest(_FakeSetupMixin, unittest.TestCase):
    def test_buffered_timeout_raises_without_partial_output(self) -> None:
        # Buffered path discards output; must not set partial_output (server.rebuild's getattr default relies on this).
        with self.assertRaises(TimeoutError) as cm:
            asyncio.run(cli.run_icedos("gc", timeout=1))
        self.assertIn("timed out after 1s", str(cm.exception))
        self.assertFalse(hasattr(cm.exception, "partial_output"))


class SuccessTest(_FakeSetupMixin, unittest.TestCase):
    body = "echo some-output\nexit 0\n"

    def test_streaming_success_returns_output(self) -> None:
        rc, out, err = asyncio.run(cli.run_icedos_streaming("gc", timeout=5))
        self.assertEqual(rc, 0)
        self.assertIn("some-output", out)
        self.assertEqual(err, "")

    def test_buffered_success_returns_output(self) -> None:
        rc, out, _ = asyncio.run(cli.run_icedos("gc", timeout=5))
        self.assertEqual(rc, 0)
        self.assertIn("some-output", out)


class SummarizeLogTailTest(unittest.TestCase):
    def test_bounds_huge_log(self) -> None:
        # thousands of built/warning lines must still produce a capped tail —
        # the regression the summarize_log_tail helper exists to prevent.
        huge = "\n".join(f"{i} built /nix/store/foo-{i}" for i in range(5000))
        out = cli.summarize_log_tail(huge)
        self.assertLessEqual(len(out.splitlines()), 40)
        self.assertIn("4999 built", out)

    def test_strips_ansi_and_redraw_frames(self) -> None:
        log = "⠋ frame\r\x1b[2K⏱ 00:01:42\nsome line\n\x1b[32mgreen\x1b[0m\n"
        out = cli.summarize_log_tail(log)
        self.assertIn("some line", out)
        self.assertIn("green", out)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("⏱", out)

    def test_empty_input(self) -> None:
        self.assertEqual(cli.summarize_log_tail(""), "")


class SummarizeBuildTest(unittest.TestCase):
    def test_caps_summary_and_keeps_trailing_error(self) -> None:
        # Tail slice must keep the real error (not the first 40 `built` lines).
        lines = [f"{i} built /nix/store/foo-{i}" for i in range(100)]
        lines.append("error: final")
        out = cli.summarize_build("\n".join(lines), "")
        body = out.splitlines()
        # 101 keep-lines → 40 kept → 61 elided
        self.assertLessEqual(len(body), 41)
        self.assertTrue(body[0].startswith("… (61 lines elided"))
        self.assertIn("error: final", out)

    def test_fallback_tail_without_summary_keywords(self) -> None:
        out = cli.summarize_build("\n".join(f"line {i}" for i in range(10)), "")
        self.assertLessEqual(len(out.splitlines()), 40)
        self.assertIn("line 9", out)


if __name__ == "__main__":
    unittest.main()
