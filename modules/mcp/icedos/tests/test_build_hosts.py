"""Tests for remote build hosts: probe parsing, host choice, leases, builds and cleanup.

Fake `ssh`, `sh` probes and `nix` binaries stand in for real hosts.

    PYTHONPATH=src python -m unittest tests.test_build_hosts -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from icedos_mcp import build_hosts, server

PROBE_OUT = """{cores}
{load} 1.00 1.00 1/500 1234
/dev/nvme0n1p2 {kb} 1 {kb} 50% /nix
some avg10={cpu} avg60=0.00 avg300=0.00 total=1
full avg10=0.00 avg60=0.00 avg300=0.00 total=1
some avg10={io} avg60=0.00 avg300=0.00 total=1
full avg10=99.00 avg60=0.00 avg300=0.00 total=1
"""


def probe(cores=12, load=1.0, gb=100, cpu=0.0, io=0.0) -> str:
    return PROBE_OUT.format(cores=cores, load=load, kb=gb * 1024 * 1024, cpu=cpu, io=io)


class _EnvMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        sandboxes = self.tmp / "sandboxes"
        sandboxes.mkdir(mode=0o700)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        cfg = self.tmp / "build.json"
        cfg.write_text(json.dumps({"hosts": ["a@h1", "b@h2"], "minFreeGb": 20, "pressureLimit": 20}))
        self._env = mock.patch.dict(os.environ, {
            "ICEDOS_SANDBOX_DIR": str(sandboxes),
            build_hosts.CONFIG_ENV: str(cfg),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        })
        self._env.start()
        build_hosts._created.clear()

    def tearDown(self) -> None:
        self._env.stop()
        build_hosts._created.clear()
        self._tmp.cleanup()

    def fake(self, name: str, body: str) -> None:
        exe = self.bin / name
        exe.write_text(f"#!{shutil.which('bash')}\n{body}\n")
        exe.chmod(0o755)


class ParseTest(unittest.TestCase):
    def test_parse_probe(self) -> None:
        p = build_hosts.parse_probe(probe(cores=16, load=3.5, gb=175, cpu=4.0, io=31.5))
        self.assertEqual(p, {"cores": 16, "load1": 3.5, "free_gb": 175.0, "pressure": 31.5})

    def test_full_pressure_ignored(self) -> None:
        self.assertEqual(build_hosts.parse_probe(probe())["pressure"], 0.0)


class ChooseTest(_EnvMixin, unittest.TestCase):
    def choose(self, local: str, h1: str | None, h2: str | None, need_gb: float = 0) -> dict:
        outputs = {"sh": local, "a@h1": h1, "b@h2": h2}

        async def fake_probe(argv, timeout=15):
            key = "sh" if argv[0] == "sh" else argv[-2]
            out = outputs[key]
            if out is None:
                return {"reachable": False, "error": "timeout"}
            return {"reachable": True, **build_hosts.parse_probe(out)}

        with mock.patch.object(build_hosts, "_probe", fake_probe):
            return asyncio.run(build_hosts.choose(need_gb))

    def test_idle_pc_stays_local(self) -> None:
        r = self.choose(probe(load=2), probe(cores=16), probe())
        self.assertEqual(r["pick"], "local")
        self.assertFalse(r["local"]["busy"])

    def test_busy_pc_picks_idlest_host(self) -> None:
        r = self.choose(probe(load=13), probe(cores=16, load=4), probe(cores=12, load=1))
        self.assertEqual(r["pick"], "b@h2")

    def test_pressure_counts_as_busy(self) -> None:
        r = self.choose(probe(load=1, io=25), probe(cores=16), None)
        self.assertEqual(r["pick"], "a@h1")
        self.assertIn("pressure", r["local"]["reason"])

    def test_host_rules(self) -> None:
        r = self.choose(probe(load=20), probe(cores=16, load=8), probe(gb=14))
        self.assertEqual(r["pick"], "local")
        self.assertIn("no host qualifies", r["why"])
        reasons = {h["host"]: h["reason"] for h in r["hosts"]}
        self.assertIn("half", reasons["a@h1"])
        self.assertIn("needs 20", reasons["b@h2"])

    def test_need_gb_adds_to_minimum(self) -> None:
        r = self.choose(probe(load=20), probe(gb=30), None, need_gb=15)
        self.assertEqual(r["pick"], "local")

    def test_running_builds_spread_hosts(self) -> None:
        with build_hosts.lease("a@h1"):
            r = self.choose(probe(load=20), probe(cores=16, load=0), probe(cores=12, load=5))
        self.assertEqual(r["pick"], "b@h2")
        self.assertEqual(r["hosts"][0]["active_builds"], 1)


class LeaseTest(_EnvMixin, unittest.TestCase):
    def test_lease_counts_and_releases(self) -> None:
        with build_hosts.lease("a@h1"), build_hosts.lease("a@h1"):
            self.assertEqual(build_hosts.active_builds(), {"a@h1": 2})
        self.assertEqual(build_hosts.active_builds(), {})

    def test_dead_pid_lease_dropped(self) -> None:
        stale = build_hosts._leases_dir() / "a%40h1.999999999.dead"
        stale.touch()
        self.assertEqual(build_hosts.active_builds(), {})
        self.assertFalse(stale.exists())


class BuildTest(_EnvMixin, unittest.TestCase):
    def test_argv(self) -> None:
        self.assertEqual(build_hosts.build_argv("nixpkgs#hello", "local"),
                         ["nix", "build", "--no-link", "--json", "nixpkgs#hello"])
        self.assertEqual(build_hosts.build_argv("x#y", "a@h1")[2:6],
                         ["--eval-store", "auto", "--store", "ssh-ng://a@h1"])

    def test_remote_build_records_only_new_paths(self) -> None:
        log = self.tmp / "argv"
        # Fake nix: `derivation show` answers in the 2.34 layout, `build` prints a log and the JSON.
        self.fake("nix", f"""
if [ "$1" = derivation ]; then
  echo '{{"derivations":{{"aaa-dep.drv":{{"outputs":{{"out":{{"path":"bbb-dep"}}}}}}}}}}'
  exit 0
fi
echo "$*" > {log}
echo "copying path '/nix/store/ccc-src' from 'https://cache.nixos.org'..." >&2
echo "building '/nix/store/aaa-dep.drv'..." >&2
echo '[{{"drvPath":"/nix/store/ddd-top.drv","outputs":{{"out":"/nix/store/eee-top"}}}}]'
""")
        ctx = mock.MagicMock()
        out = asyncio.run(server.nix_build(ctx, "nixpkgs#hello", host="a@h1"))
        self.assertIn("Build succeeded on a@h1", out)
        self.assertIn("output: /nix/store/eee-top", out)
        self.assertIn("--store ssh-ng://a@h1", log.read_text())
        # The top output may have existed already, so only logged arrivals count.
        self.assertEqual(build_hosts._created["a@h1"],
                         {"/nix/store/ccc-src", "/nix/store/aaa-dep.drv", "/nix/store/bbb-dep"})
        self.assertEqual(build_hosts.active_builds(), {})

    def test_unknown_host_refused(self) -> None:
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), "x#y", host="evil@h3")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_flag_installable_refused(self) -> None:
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), "--impure", host="local")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_cleanup_skips_live_paths(self) -> None:
        calls = self.tmp / "calls"
        self.fake("nix", f"""
echo "$*" >> {calls}
for a in "$@"; do
  if [ "$a" = /nix/store/live ]; then
    echo "error: Cannot delete path '/nix/store/live' since it is still alive." >&2
    exit 1
  fi
done
""")
        build_hosts._created["a@h1"] = {"/nix/store/live", "/nix/store/dead"}
        report = build_hosts.cleanup()
        self.assertIn("deleted 1 paths", report["a@h1"])
        self.assertIn("kept 1 live", report["a@h1"])
        self.assertEqual(build_hosts._created, {})
        last = calls.read_text().splitlines()[-1]
        self.assertEqual(last, "store delete --store ssh-ng://a@h1 /nix/store/dead")


class RebuildFlagTest(unittest.TestCase):
    def test_builder_live_only(self) -> None:
        err = server._check_rebuild_flags(["--builder", "a@h1"], in_sandbox=True)
        self.assertIn("nix_build", err or "")
        self.assertIsNone(server._check_rebuild_flags(["--builder", "a@h1"], in_sandbox=False))


if __name__ == "__main__":
    unittest.main()
