"""Tests for shell_run: argument checks, local and remote runs, fetch and cleanup.

Fake `ssh`, `nix`, `nix-instantiate` and `nix-shell` binaries stand in for hosts and Nix;
the fake ssh runs the remote command on this machine.

    PYTHONPATH=src python -m unittest tests.test_shell_run -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from icedos_mcp import build_hosts, server, settings
from icedos_mcp import shell_run as shells

from .test_build_hosts import _EnvMixin

DRV = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nix-shell.drv"


class _ShellMixin(_EnvMixin):
    def setUp(self) -> None:
        super().setUp()
        shells._dirs.clear()
        self.root = self.tmp / "remote-tmp"
        self._root = mock.patch.object(shells, "remote_root", lambda: str(self.root))
        self._root.start()
        rsync = shutil.which("rsync")
        assert rsync, "tests need rsync"
        self._rsync = mock.patch.dict(os.environ, {"ICEDOS_MCP_RSYNC": rsync})
        self._rsync.start()
        self.log = self.tmp / "calls.log"
        self.fake("ssh", f'while [ "$1" = -o ] || [ "$1" = -l ]; do shift 2; done\necho "ssh $1 ${{*:2}}" >> {self.log}\n'
                         'shift\nexec sh -c "$*"')
        self.fake("nix", f'echo "nix $*" >> {self.log}\n'
                         f'[ "$1" = copy ] && echo "copying path \'{DRV}\' to \'x\'..." >&2\n'
                         f'[ "$1" = path-info ] && echo {DRV}\ntrue')
        self.fake("nix-instantiate", f'echo "nix-instantiate $*" >> {self.log}\necho {DRV}')
        self.fake("nix-shell", '[ "$2" = --run ] && exec sh -c "$3"')
        self.src = self.tmp / "proj"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "main.rs").write_text("fn main() {}")
        (self.src / ".gitignore").write_text("target/\n")
        (self.src / "target").mkdir()
        (self.src / "target" / "big").write_text("x")
        self.ctx = mock.Mock()
        self.ctx.report_progress = mock.AsyncMock()

    def tearDown(self) -> None:
        self._rsync.stop()
        self._root.stop()
        shells._dirs.clear()
        super().tearDown()

    def run_tool(self, **kw) -> str:
        kw.setdefault("cwd", str(self.src))
        kw.setdefault("packages", ["cargo"])
        return asyncio.run(server.shell_run(self.ctx, **kw))


class ArgsTest(unittest.TestCase):
    def test_check_args(self) -> None:
        self.assertIn("exactly one", shells.check_args([], "", []))
        self.assertIn("exactly one", shells.check_args(["a"], "/x", []))
        self.assertIn("attribute path", shells.check_args(["a; rm -rf /"], "", []))
        self.assertEqual(shells.check_args(["python3Packages.pytest", "gcc"], "", []), "")
        self.assertIn("relative", shells.check_args(["a"], "", ["../x"]))
        self.assertIn("relative", shells.check_args(["a"], "", ["/etc/passwd"]))

    def test_flake_ref_defaults_to_devshell(self) -> None:
        self.assertEqual(shells.flake_ref("/p#devShells.x.ci"), "/p#devShells.x.ci")
        self.assertTrue(shells.flake_ref("/p").startswith("/p#devShells."))

    def test_remote_command_quotes_input(self) -> None:
        cmd = shells.remote_command("/tmp/w", DRV, "echo 'a b' $HOME; touch x")
        self.assertTrue(cmd.startswith("setsid -w sh -c "))
        self.assertIn("'echo '\"'\"'a b'\"'\"' $HOME; touch x'", cmd)


class LocalRunTest(_ShellMixin, unittest.TestCase):
    def test_remote_exec_off_stays_local(self) -> None:
        self.configure("build", remoteExec=False)
        out = self.run_tool(command="pwd; echo ok")
        self.assertIn("succeeded here (build.remoteExec is off)", out)
        self.assertIn(str(self.src), out)
        self.assertNotIn("ssh", self.log.read_text())

    def test_host_needs_remote_exec(self) -> None:
        self.configure("build", remoteExec=False)
        self.assertIn("remoteExec", self.run_tool(command="true", host="a@h1"))
        self.assertIn("unknown host", self.run_tool(command="true", host="z@h9"))

    def test_scratch_on_ssd(self) -> None:
        out = self.run_tool(command='echo "$ICEDOS_SCRATCH|$TMPDIR"', host="local", need_gb=10)
        scratch = f"{self.ssd}/ai-dump/shell-run-{shells.key(str(self.src))}"
        self.assertIn(f"{scratch}|{scratch}/tmp", out)
        self.assertEqual(shells._dirs["local"], {scratch})
        self.assertEqual(shells.cleanup(), {"local work dirs": "deleted 1"})
        self.assertFalse(Path(scratch).exists())

    def test_failure_reports_exit(self) -> None:
        self.assertIn("failed (exit 3)", self.run_tool(command="exit 3", host="local"))


class RemoteRunTest(_ShellMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure("build", remoteExec=True)
        self.work = self.root / shells.key(str(self.src))

    def test_sync_run_fetch_and_cleanup(self) -> None:
        out = self.run_tool(command="mkdir -p target && echo built > target/out && cat main.rs",
                            host="a@h1", fetch=["target/out"])
        self.assertIn(f"succeeded on a@h1, in {self.work}/src", out)
        self.assertIn("fn main() {}", out)
        self.assertIn("Fetched target/out", out)
        self.assertEqual((self.src / "target" / "out").read_text(), "built\n")
        # .gitignore'd files are not synced, and stay on the host between runs.
        self.assertFalse((self.work / "src" / "target" / "big").exists())
        self.assertTrue((self.work / "src" / "target" / "out").exists())
        self.assertIn("nix copy --substitute-on-destination --to ssh-ng://a@h1", self.log.read_text())
        self.assertIn(DRV, build_hosts._created["ssh-ng://a@h1"])
        out = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertEqual(out["a@h1 work dirs"], "deleted 1")
        self.assertFalse(self.work.exists())

    def test_rerun_keeps_outputs_and_deletes_removed_sources(self) -> None:
        self.run_tool(command="mkdir -p target && touch target/cache", host="a@h1")
        (self.src / "main.rs").unlink()
        out = self.run_tool(command="ls; ls target", host="a@h1")
        self.assertNotIn("main.rs", out)
        self.assertIn("cache", out)

    def test_refuses_without_room(self) -> None:
        out = self.run_tool(command="true", host="a@h1", need_gb=10**9)
        self.assertIn("GB free", out)

    def test_timeout_kills_remote_group(self) -> None:
        out = self.run_tool(command="sleep 30 & sleep 30", host="a@h1", timeout=2)
        self.assertIn('"timeout"', out)
        self.assertIn("kill -TERM", self.log.read_text())




class HostDriveRunTest(_ShellMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure("build", remoteExec=True)
        self.host_ssd = str(self.tmp / "host-ssd")
        Path(self.host_ssd).mkdir()
        self.host_cfg = self.tmp / "host-mcp-settings.json"
        self.host_cfg.write_text(json.dumps({
            "storage": {"drives": {self.host_ssd: "random"}, "priority": [self.host_ssd], "reserveGb": 1}
        }))
        self.fake("mountpoint", "exit 0")
        self._host_file = mock.patch.object(settings, "HOST_FILE", str(self.host_cfg))
        self._host_file.start()
        settings._hosts.clear()

    def tearDown(self) -> None:
        settings._hosts.clear()
        self._host_file.stop()
        super().tearDown()

    def test_drive_picked(self) -> None:
        work = Path(self.host_ssd) / "ai-dump" / f"shell-run-{shells.key(str(self.src))}"
        out = self.run_tool(command="pwd; echo ok", host="a@h1")
        self.assertIn(f"succeeded on a@h1, in {work}/src", out)
        self.assertTrue((work / "src").is_dir())
        self.assertIn(str(work), shells._dirs.get("a@h1", set()))

    def test_no_drives_falls_back_to_tmp(self) -> None:
        self.host_cfg.write_text(json.dumps({"storage": {"drives": {}, "priority": []}}))
        settings._hosts.clear()
        work = self.root / shells.key(str(self.src))
        out = self.run_tool(command="pwd; echo ok", host="a@h1")
        self.assertIn(f"succeeded on a@h1, in {work}/src", out)
        self.assertTrue((work / "src").is_dir())
        self.assertIn(str(work), shells._dirs.get("a@h1", set()))

    def test_repeat_run_reuses_same_dir(self) -> None:
        work = Path(self.host_ssd) / "ai-dump" / f"shell-run-{shells.key(str(self.src))}"
        self.run_tool(command="mkdir -p target && touch target/cache_marker", host="a@h1")
        self.assertTrue((work / "src" / "target" / "cache_marker").exists())
        out = self.run_tool(command="ls target", host="a@h1")
        self.assertIn(f"succeeded on a@h1, in {work}/src", out)
        self.assertIn("cache_marker", out)

    def test_restart_reuses_existing_dir_after_server_restart(self) -> None:
        ssd2 = str(self.tmp / "host-ssd2")
        Path(ssd2).mkdir()
        work1 = Path(self.host_ssd) / "ai-dump" / f"shell-run-{shells.key(str(self.src))}"
        self.run_tool(command="mkdir -p target && touch target/saved", host="a@h1")
        self.assertTrue((work1 / "src" / "target" / "saved").exists())
        shells._dirs.clear()
        self.host_cfg.write_text(json.dumps({
            "storage": {
                "drives": {self.host_ssd: "random", ssd2: "random"},
                "priority": [ssd2, self.host_ssd],
                "reserveGb": 1,
            }
        }))
        settings._hosts.clear()
        out = self.run_tool(command="ls target", host="a@h1")
        self.assertIn(f"succeeded on a@h1, in {work1}/src", out)
        self.assertIn("saved", out)
        work2 = Path(ssd2) / "ai-dump" / f"shell-run-{shells.key(str(self.src))}"
        self.assertFalse(work2.exists())

    def test_cleanup_removes_drive_work_dir(self) -> None:
        work = Path(self.host_ssd) / "ai-dump" / f"shell-run-{shells.key(str(self.src))}"
        self.run_tool(command="echo hi", host="a@h1")
        self.assertTrue(work.exists())
        out = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertEqual(out["a@h1 work dirs"], "deleted 1")
        self.assertFalse(work.exists())
        self.assertEqual(shells._dirs.get("a@h1", set()), set())

if __name__ == "__main__":
    unittest.main()
