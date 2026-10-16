"""Tests for flake copy preflight and GC root cleanup, with fake `nix` and `nix-store` binaries.

    PYTHONPATH=src python -m unittest tests.test_leftovers -v
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import unittest
from unittest import mock

from icedos_mcp import build_hosts, leftovers, server
from tests.test_build_hosts import _EnvMixin


class FlakeDirTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(leftovers.flake_dir("path:/a/b#x"), ("/a/b", "path"))
        self.assertEqual(leftovers.flake_dir("path:/a/b?dir=c"), ("/a/b", "path"))
        self.assertEqual(leftovers.flake_dir("git+file:///a/b#x"), ("/a/b", "git"))
        self.assertEqual(leftovers.flake_dir("/a/b#x"), ("/a/b", ""))
        self.assertIsNone(leftovers.flake_dir("nixpkgs#hello"))
        self.assertIsNone(leftovers.flake_dir("github:o/r"))
        self.assertIsNone(leftovers.flake_dir("/nix/store/abc-hello"))
        with self.assertRaises(ValueError):
            leftovers.flake_dir(".#x")


class PreflightTest(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.tmp / "proj"
        self.dir.mkdir()
        (self.dir / "flake.nix").write_text("{ outputs = _: {}; }\n")
        self._limit = mock.patch.object(leftovers, "settings", lambda: {"maxFlakeCopyMb": 2})
        self._limit.start()

    def tearDown(self) -> None:
        self._limit.stop()
        super().tearDown()

    def test_path_flake_too_big(self) -> None:
        nm = self.dir / "node_modules"
        nm.mkdir()
        (nm / "blob").write_bytes(b"x" * 3 * 2**20)
        r = leftovers.preflight(str(self.dir))
        self.assertEqual((r["mode"], r["verdict"]), ("path", "refuse"))
        self.assertIn(str(nm), r["heavy"])
        self.assertIn("git init", r["reason"])

    def test_small_path_flake_ok(self) -> None:
        r = leftovers.preflight(f"path:{self.dir}#x")
        self.assertEqual(r["verdict"], "ok")

    def test_no_flake_nix(self) -> None:
        (self.dir / "flake.nix").unlink()
        self.assertEqual(leftovers.preflight(str(self.dir))["verdict"], "refuse")

    def test_git_flake_ignores_untracked_and_ignored(self) -> None:
        run = lambda *a: subprocess.run(["git", "-C", str(self.dir), *a], check=True, capture_output=True)
        run("init", "-q")
        (self.dir / ".gitignore").write_text("node_modules\n")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules" / "blob").write_bytes(b"x" * 3 * 2**20)
        run("add", "flake.nix", ".gitignore")
        (self.dir / "new.nix").write_text("{}")
        r = leftovers.preflight(str(self.dir))
        self.assertEqual((r["mode"], r["verdict"]), ("git", "ok"))
        self.assertEqual(r["untracked"], ["new.nix"])
        # path: ignores git, so the ignored blob counts again.
        self.assertEqual(leftovers.preflight(f"path:{self.dir}")["verdict"], "refuse")

    def test_nix_build_refuses_then_allows(self) -> None:
        (self.dir / "big").write_bytes(b"x" * 3 * 2**20)
        self.fake("nix", """
if [ "$1" = flake ]; then echo '{"path":"/nix/store/sss-source"}'; fi
if [ "$1" = build ]; then echo '[{"drvPath":"/nix/store/d.drv","outputs":{"out":"/nix/store/o"}}]'; fi
""")
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), f"{self.dir}#x", host="local")))
        self.assertEqual(out["error"]["kind"], "flake_copy")
        out = asyncio.run(server.nix_build(mock.MagicMock(), f"{self.dir}#x", host="local", allow_copy=True))
        self.assertIn("Build succeeded", out)
        self.assertEqual(build_hosts._created[leftovers.HOST_STORE], {"/nix/store/sss-source"})


class RootsTest(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / "p").mkdir(parents=True)
        self.result = self.home / "p" / "result"
        self.result.symlink_to("/nix/store/aaa-hello")
        self._home = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._home.start()
        self.argv = self.tmp / "argv"
        h = self.home
        self.fake("nix-store", f"""
[ -L {self.result} ] && echo '"{self.result}" -> /nix/store/aaa-hello'
echo '"{h}/.local/state/home-manager/gcroots/current-home" -> /nix/store/bbb-hm'
echo '"{h}/.cache/nix/flake-registry.json" -> /nix/store/ccc-reg'
echo '"/nix/var/nix/profiles/system-1-link" -> /nix/store/ddd-sys'
echo '"/mnt/games/x/.gcroots/app" -> /nix/store/eee-app'
echo '/proc/1/maps -> /nix/store/fff-lib'
""")
        self.fake("nix", f"""
echo "$*" >> {self.argv}
if [ "$1" = path-info ]; then echo '{{"/nix/store/aaa-hello":{{"closureSize":{5 * 2**20}}}}}'; fi
""")

    def tearDown(self) -> None:
        self._home.stop()
        super().tearDown()

    def test_lists_only_leftover_candidates(self) -> None:
        rows = {r["link"]: r for r in leftovers.roots()}
        self.assertEqual(set(rows), {str(self.result), "/mnt/games/x/.gcroots/app"})
        r = rows[str(self.result)]
        self.assertEqual((r["kind"], r["removable"], r["closure_mb"]), ("result", True, 5))
        self.assertFalse(rows["/mnt/games/x/.gcroots/app"]["removable"])
        self.assertEqual([x["link"] for x in leftovers.roots(under=str(self.home / "p"))], [str(self.result)])

    def test_cleanup_unlinks_and_deletes(self) -> None:
        out = json.loads(asyncio.run(server.nix_cleanup(
            [str(self.result), "/mnt/games/x/.gcroots/app", "/nix/var/nix/profiles/system-1-link"], collect=True,
        )))
        self.assertFalse(self.result.is_symlink())
        self.assertEqual(out["removed"], [str(self.result)])
        self.assertEqual(out["deleted"], ["/nix/store/aaa-hello"])
        self.assertEqual(set(out["refused"]), {"/mnt/games/x/.gcroots/app", "/nix/var/nix/profiles/system-1-link"})
        log = self.argv.read_text()
        self.assertIn("store delete --store auto /nix/store/aaa-hello", log)
        self.assertIn("store gc\n", log)
        self.assertNotIn("-d", log.split())


if __name__ == "__main__":
    unittest.main()
