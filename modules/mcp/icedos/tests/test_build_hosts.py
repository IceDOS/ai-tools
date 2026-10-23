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

from icedos_mcp import build_hosts, server, settings, storage

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
        self.ssd, self.ssd2, self.hdd = (str(self.tmp / n) for n in ("ssd", "ssd2", "hdd"))
        for d in (self.ssd, self.ssd2, self.hdd):
            Path(d).mkdir()
        self.cfg = cfg = self.tmp / "settings.json"
        cfg.write_text(json.dumps({
            "build": {"hosts": ["a@h1", "b@h2"], "minFreeGb": 20, "pressureLimit": 20},
            "storage": {
                "drives": {self.ssd: "random", self.ssd2: "random", self.hdd: "sequential"},
                "reserveGb": 30, "largeGb": 5,
            },
        }))
        self._env = mock.patch.dict(os.environ, {
            "ICEDOS_SANDBOX_DIR": str(sandboxes),
            settings.ENV: str(cfg),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        })
        self._env.start()
        # Free GB per mount; tests change it to steer drive choice.
        self.free = {"/": 100.0, self.ssd: 100.0, self.ssd2: 60.0, self.hdd: 500.0}
        self._space = mock.patch.object(storage, "_space", lambda p: (self.free[p], 1000.0))
        self._mount = mock.patch.object(storage.os.path, "ismount", lambda p: p in self.free)
        self._space.start()
        self._mount.start()
        build_hosts._created.clear()

    def tearDown(self) -> None:
        self._space.stop()
        self._mount.stop()
        self._env.stop()
        build_hosts._created.clear()
        self._tmp.cleanup()

    def configure(self, section: str, **values) -> None:
        data = json.loads(self.cfg.read_text())
        data[section].update(values)
        self.cfg.write_text(json.dumps(data))

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

    def test_busy_pc_picks_first_listed_host(self) -> None:
        r = self.choose(probe(load=13), probe(cores=16, load=4), probe(cores=12, load=1))
        self.assertEqual(r["pick"], "a@h1")
        self.assertEqual(r["order"], ["local", "a@h1", "b@h2"])

    def test_local_entry_sets_priority(self) -> None:
        self.configure("build", hosts=["b@h2", "a@h1", "local"])
        r = self.choose(probe(load=1), probe(), probe())
        self.assertEqual(r["pick"], "b@h2")
        self.assertIn("before this PC", r["why"])
        self.assertEqual(build_hosts.settings()["hosts"], ["b@h2", "a@h1"])
        r = self.choose(probe(load=1), probe(load=8), None)
        self.assertEqual(r["pick"], "local")
        self.assertEqual(r["why"], "this PC is not busy")

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
        self.assertEqual(build_hosts._created["ssh-ng://a@h1"],
                         {"/nix/store/ccc-src", "/nix/store/aaa-dep.drv", "/nix/store/bbb-dep"})
        self.assertEqual(build_hosts.active_builds(), {})

    def test_unknown_host_refused(self) -> None:
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), "x#y", host="evil@h3")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_flag_installable_refused(self) -> None:
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), "--impure", host="local")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def fake_store(self, refs: dict[str, list[str]], live: frozenset[str] | set[str] = frozenset()) -> Path:
        """Fake nix whose path-info answers refs and whose delete refuses live paths or alive referrers."""
        calls = self.tmp / "calls"
        info = {p: {"references": r} for p, r in refs.items()}
        (self.tmp / "info.json").write_text(json.dumps(info))
        # A path is alive while it is live or something not being deleted in this call references it.
        checks = "\n".join(
            f'  [ "$a" = {p} ] && echo "error: Cannot delete path \'{p}\' since it is still alive." >&2 && exit 1'
            for p in sorted(live)
        )
        referrers = "\n".join(
            f'  [ "$a" = {d} ] && ! printf "%s\\n" "$@" | grep -qx {q} && grep -qx {q} {self.tmp}/valid '
            f'&& echo "error: Cannot delete path \'{d}\' since it is still alive." >&2 && exit 1'
            for q, ds in refs.items() for d in ds if d != q
        )
        (self.tmp / "valid").write_text("\n".join(refs) + "\n")
        self.fake("nix", f"""
echo "$*" >> {calls}
if [ "$1" = path-info ]; then cat {self.tmp}/info.json; exit 0; fi
if [ "$1 $2" = "store delete" ]; then
  for a in "$@"; do
{checks or "    :"}
{referrers or "    :"}
  done
  for a in "$@"; do sed -i "\\|^$a$|d" {self.tmp}/valid; done
fi
""")
        return calls

    def test_cleanup_skips_live_paths(self) -> None:
        calls = self.fake_store({"/nix/store/live": [], "/nix/store/dead": []}, live={"/nix/store/live"})
        build_hosts._created["ssh-ng://a@h1"] = {"/nix/store/live", "/nix/store/dead"}
        report = build_hosts.cleanup()
        self.assertIn("deleted 1 paths", report["ssh-ng://a@h1"])
        self.assertIn("kept 1 live", report["ssh-ng://a@h1"])
        # Kept paths stay recorded for a later cleanup.
        self.assertEqual(build_hosts._created, {"ssh-ng://a@h1": {"/nix/store/live"}})
        last = calls.read_text().splitlines()[-1]
        self.assertEqual(last, "store delete --store ssh-ng://a@h1 /nix/store/dead")

    def test_cleanup_deletes_referrers_first(self) -> None:
        top, lib, libc = "/nix/store/top", "/nix/store/lib", "/nix/store/libc"
        calls = self.fake_store({top: [lib, libc, top], lib: [libc], libc: []})
        build_hosts._created["ssh-ng://a@h1"] = {top, lib, libc}
        report = build_hosts.cleanup()
        self.assertEqual(report["ssh-ng://a@h1"], "deleted 3 paths")
        deletes = [c.split()[-1] for c in calls.read_text().splitlines() if c.startswith("store delete")]
        self.assertEqual(deletes, [top, lib, libc])


class StorageTest(_EnvMixin, unittest.TestCase):
    def test_pick_prefers_io_then_room(self) -> None:
        r = storage.pick(20, "random", "proj")
        self.assertEqual(r["mount"], self.ssd)
        self.assertTrue(Path(r["dir"]).is_dir())
        self.assertEqual(storage.pick(20, "sequential")["mount"], self.hdd)

    def test_pick_falls_back_and_keeps_reserve(self) -> None:
        self.free.update({self.ssd: 40.0, self.ssd2: 40.0})
        r = storage.pick(20, "random")
        self.assertEqual(r["mount"], self.hdd)
        self.assertFalse(r["matches_io"])
        with self.assertRaises(ValueError):
            storage.pick(1000)

    def test_pick_follows_priority(self) -> None:
        self.configure("storage", priority=[self.ssd2 + "/", self.hdd])
        self.assertEqual(storage.pick(20, "random")["mount"], self.ssd2)
        self.free[self.ssd2] = 40.0
        self.assertEqual(storage.pick(20, "random")["mount"], self.ssd)

    def test_store_for_host_last(self) -> None:
        self.configure("storage", priority=[self.ssd2, "host"])
        path, why = storage.store_for(1)
        self.assertEqual(path, storage.store_dir(self.ssd2))
        self.assertIn("before the host store", why)
        # Unlisted mounts rank after every listed entry, the host store included.
        self.free[self.ssd2] = 10.0
        self.assertEqual(storage.store_for(1)[0], "")
        self.assertEqual(storage.store_for(10)[0], storage.store_dir(self.ssd))

    def test_pick_rejects_bad_project(self) -> None:
        for bad in ("../x", ".hidden", "a/b"):
            with self.assertRaises(ValueError):
                storage.pick(1, project=bad)

    def test_store_for(self) -> None:
        self.assertEqual(storage.store_for(1)[0], "")
        # Large builds go to an SSD, preferring one that already has a store.
        Path(storage.store_dir(self.ssd2)).mkdir(parents=True)
        self.assertEqual(storage.store_for(10)[0], storage.store_dir(self.ssd2))
        # A small build that would eat into the root reserve moves too.
        self.free["/"] = 31.0
        self.assertEqual(storage.store_for(2)[0], storage.store_dir(self.ssd2))
        self.assertEqual(storage.store_for(10, "host")[0], "")
        self.assertEqual(storage.store_for(1, self.hdd)[0], storage.store_dir(self.hdd))
        with self.assertRaises(ValueError):
            storage.store_for(1, "/mnt/elsewhere")
        self.free.update({self.ssd: 10.0, self.ssd2: 10.0})
        with self.assertRaises(ValueError):
            storage.store_for(10)

    def test_drive_store_build_and_cleanup(self) -> None:
        argv = self.tmp / "argv"
        self.fake("nix", f"""
echo "$*" >> {argv}
if [ "$1" = path-info ]; then
  echo '{{"/nix/store/ddd-top.drv":{{"references":[]}},"/nix/store/eee-top":{{"references":[]}}}}'
fi
if [ "$1" = build ]; then
  mkdir -p "$3"
  echo '[{{"drvPath":"/nix/store/ddd-top.drv","outputs":{{"out":"/nix/store/eee-top"}}}}]'
fi
""")
        store = storage.store_dir(self.ssd)
        out = asyncio.run(server.nix_build(mock.MagicMock(), "nixpkgs#hello", host="local", need_gb=10))
        self.assertIn(f"on disk: {store}/nix/store/eee-top", out)
        self.assertIn(f"nix shell --store {store} nixpkgs#hello", out)
        self.assertIn(f"build --store {store} --no-link", argv.read_text())
        # Everything in a drive store came from agents, so the result's paths count.
        self.assertEqual(build_hosts._created[store], {"/nix/store/ddd-top.drv", "/nix/store/eee-top"})
        report = json.loads(asyncio.run(server.build_cleanup(self.ssd, gc=True)))
        self.assertIn("deleted 2 paths", report[store])
        self.assertIn(f"gc {store}", report)
        self.assertIn(f"store gc --store {store}", argv.read_text())

    def test_store_needs_local_build(self) -> None:
        out = json.loads(asyncio.run(server.nix_build(mock.MagicMock(), "x#y", host="a@h1", store=self.ssd)))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_cleanup_rejects_unknown_target(self) -> None:
        out = json.loads(asyncio.run(server.build_cleanup("/mnt/games")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_status_sizes_dumps(self) -> None:
        d = Path(self.hdd) / "ai-dump" / "proj"
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"x" * 3 * 1024 * 1024)
        r = asyncio.run(storage.status())
        hdd = next(x for x in r["drives"] if x["mount"] == self.hdd)
        self.assertGreaterEqual(hdd["ai_dump_mb"]["proj"], 3)
        self.assertEqual(r["root"]["room_gb"], 70.0)


class RebuildFlagTest(unittest.TestCase):
    def test_builder_live_only(self) -> None:
        err = server._check_rebuild_flags(["--builder", "a@h1"], in_sandbox=True)
        self.assertIn("nix_build", err or "")
        self.assertIsNone(server._check_rebuild_flags(["--builder", "a@h1"], in_sandbox=False))


if __name__ == "__main__":
    unittest.main()
