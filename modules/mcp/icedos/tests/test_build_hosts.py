"""Tests for remote build hosts: probe parsing, host choice, leases, builds and cleanup.

Fake `ssh`, `sh` probes and `nix` binaries stand in for real hosts.

    PYTHONPATH=src python -m unittest tests.test_build_hosts -v
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from icedos_mcp import build_hosts, cli, pkgs_host, server, settings, storage

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
        def fake_host_state(h, cfg):
            rows = [storage._row(m, io, float(cfg["reserveGb"]), (self.free.get(m, 100.0), 1000.0, False)) for m, io in cfg["drives"].items()]
            root_row = storage._row("/nix/store", "root", float(cfg["reserveGb"]), (self.free.get("/", 100.0), 1000.0, False))
            root_row.pop("nix_store", None)
            return rows, root_row
        self._host_state = mock.patch.object(storage, "_host_state", fake_host_state)
        self._host_state.start()
        self._mkdir = mock.patch.object(storage, "_mkdir", lambda h, p: Path(p).mkdir(parents=True, exist_ok=True))
        self._mkdir.start()
        for h in ["a@h1", "b@h2"]:
            settings._hosts[h] = copy.deepcopy(settings.DEFAULTS["storage"])
        build_hosts._created.clear()

    def tearDown(self) -> None:
        self._mkdir.stop()
        self._host_state.stop()
        self._space.stop()
        self._mount.stop()
        self._env.stop()
        settings._hosts.clear()
        build_hosts._created.clear()
        self._tmp.cleanup()

    def configure(self, section: str, **values) -> None:
        data = json.loads(self.cfg.read_text())
        data[section].update(values)
        self.cfg.write_text(json.dumps(data))
        pass

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

    def test_result_paths_reads_both_shapes(self) -> None:
        drv = "/nix/store/aaa-x.drv"
        shown, every = build_hosts.result_paths([
            {"drvPath": drv, "outputs": {"out": "/nix/store/bbb-x", "doc": "/nix/store/ccc-x-doc"}},
            "/nix/store/ddd-opaque",
        ])
        self.assertEqual(shown, ["/nix/store/bbb-x", "/nix/store/ccc-x-doc", "/nix/store/ddd-opaque"])
        self.assertEqual(every, {drv, "/nix/store/bbb-x", "/nix/store/ccc-x-doc", "/nix/store/ddd-opaque"})

    def test_drv_installable(self) -> None:
        drv = "/nix/store/aaa-x.drv"
        self.assertEqual(build_hosts.drv_installable(drv), drv + "^*")
        for same in (drv + "^out", drv + "^*", "/nix/store/bbb-x", "nixpkgs#hello", "/abs/flake#x"):
            self.assertEqual(build_hosts.drv_installable(same), same)


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

    def test_disk_full_qualifies_when_random_drive_has_room(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd],
            "reserveGb": 30,
            "largeGb": 5,
        }
        r = self.choose(probe(load=20), probe(cores=16, load=1.0, gb=5), None)
        h1 = next(x for x in r["hosts"] if x["host"] == "a@h1")
        self.assertTrue(h1["ok"])
        self.assertEqual(h1["drive"], self.ssd)
        self.assertEqual(r["drive"], self.ssd)
        self.assertIn("qualifies", h1["reason"])
        self.assertEqual(r["pick"], "a@h1")

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

    def test_argv_drive_store_reads_store_paths_from_host(self) -> None:
        self.assertEqual(
            build_hosts.build_argv("/nix/store/aaa-x.drv^*", "local", "/d/nix-store"),
            ["nix", "build", "--eval-store", "auto", "--store", "/d/nix-store",
             "--no-link", "--json", "/nix/store/aaa-x.drv^*"],
        )
        self.assertEqual(
            build_hosts.build_argv("nixpkgs#hello", "local", "/d/nix-store"),
            ["nix", "build", "--store", "/d/nix-store", "--no-link", "--json", "nixpkgs#hello"],
        )

    def test_plain_drv_builds_outputs(self) -> None:
        argv = self.tmp / "argv"
        self.fake("nix", f"""
echo "$*" >> {argv}
echo '[{{"drvPath":"/nix/store/aaa-x.drv","outputs":{{"out":"/nix/store/bbb-x"}}}}]'
""")
        out = asyncio.run(server.nix_build(mock.MagicMock(), "/nix/store/aaa-x.drv", host="local", store="host"))
        self.assertIn("build --no-link --json /nix/store/aaa-x.drv^*", argv.read_text())
        self.assertIn("output: /nix/store/bbb-x", out)
        self.assertIn("/nix/store/aaa-x.drv^*", out)

    def test_opaque_result_does_not_crash(self) -> None:
        # Nix 2.34 prints an opaque store path as a plain string in `nix build --json`.
        self.fake("nix", """echo '["/nix/store/eee-top"]'""")
        out = asyncio.run(server.nix_build(mock.MagicMock(), "/nix/store/eee-top", host="local", store="host"))
        self.assertIn("Build succeeded", out)
        self.assertIn("output: /nix/store/eee-top", out)

    def test_drive_store_records_opaque_result(self) -> None:
        build_hosts.record("/drive", "", ["/nix/store/eee-top"])
        self.assertEqual(build_hosts._created["/drive"], {"/nix/store/eee-top"})

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

    def test_remote_drv_build_is_cleaned_up(self) -> None:
        drv, out, src = "/nix/store/aaa-x.drv", "/nix/store/bbb-x", "/nix/store/ccc-src"
        calls = self.tmp / "calls"
        # `derivation show` uses the 2.34 layout; path-info and store delete always succeed.
        self.fake("nix", f"""
echo "$*" >> {calls}
case "$1" in
  derivation) echo '{{"derivations":{{"aaa-x.drv":{{"outputs":{{"out":{{"path":"bbb-x"}}}}}}}}}}' ;;
  build)
    echo "copying path '{src}' to 'ssh-ng://a@h1'..." >&2
    echo "building '{drv}'..." >&2
    echo '[{{"drvPath":"{drv}","outputs":{{"out":"{out}"}}}}]' ;;
  path-info) echo '{{"{drv}":{{"references":[]}},"{out}":{{"references":[]}},"{src}":{{"references":[]}}}}' ;;
esac
""")
        res = asyncio.run(server.nix_build(mock.MagicMock(), drv, host="a@h1"))
        self.assertIn("Build succeeded on a@h1", res)
        self.assertIn("queued for build_cleanup", res)
        self.assertIn(f"--store ssh-ng://a@h1 --no-link --json {drv}^*", calls.read_text())
        self.assertEqual(build_hosts._created["ssh-ng://a@h1"], {drv, out, src})
        report = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertEqual(report["ssh-ng://a@h1"], "deleted 3 paths")
        self.assertNotIn("ssh-ng://a@h1", build_hosts._created)
        self.assertIn("store delete --store ssh-ng://a@h1", calls.read_text())

    def test_drive_store_drv_build_is_cleaned_up(self) -> None:
        drv, out = "/nix/store/aaa-x.drv", "/nix/store/bbb-x"
        argv = self.tmp / "argv"
        # Real Nix keeps the .drv in the host store, so path-info reports it as null here.
        self.fake("nix", f"""
echo "$*" >> {argv}
case "$1" in
  build) echo '[{{"drvPath":"{drv}","outputs":{{"out":"{out}"}}}}]' ;;
  path-info) echo '{{"{drv}":null,"{out}":{{"references":[]}}}}' ;;
esac
""")
        store = storage.store_dir(self.ssd)
        res = asyncio.run(server.nix_build(mock.MagicMock(), drv, host="local", need_gb=10))
        self.assertIn(f"build --eval-store auto --store {store} --no-link --json {drv}^*", argv.read_text())
        self.assertIn(f"output: {out} (on disk: {store}{out})", res)
        self.assertIn(f"Run it with: nix shell --store {store} {out}", res)
        report = json.loads(asyncio.run(server.build_cleanup(self.ssd)))
        self.assertEqual(report[store], "deleted 1 paths")
        self.assertNotIn(store, build_hosts._created)
        self.assertIn(f"store delete --store {store} {out}", argv.read_text())



    def test_matching_stores_does_not_prefix_match_similar_hosts(self) -> None:
        build_hosts._created["ssh-ng://a@b"] = {"/nix/store/111"}
        build_hosts._created["ssh-ng://a@b?remote-store=%2Fmnt%2Fssd"] = {"/nix/store/222"}
        build_hosts._created["ssh-ng://a@bc"] = {"/nix/store/333"}
        build_hosts._created["ssh-ng://a@bc?remote-store=%2Fmnt%2Fssd"] = {"/nix/store/444"}

        matches1 = build_hosts.matching_stores("a@b")
        matches2 = build_hosts.matching_stores("ssh-ng://a@b")
        self.assertEqual(matches1, matches2)
        self.assertEqual(set(matches1), {"ssh-ng://a@b", "ssh-ng://a@b?remote-store=%2Fmnt%2Fssd"})
        self.assertNotIn("ssh-ng://a@bc", matches1)
        self.assertNotIn("ssh-ng://a@bc?remote-store=%2Fmnt%2Fssd", matches1)

    def test_nix_build_auto_drive_fallback_picks_drive_store(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd],
            "reserveGb": 30,
            "largeGb": 5,
        }
        self.fake("ssh", f'''case "$*" in *ls*) exit 1 ;; *) cat << "EOF"\n{probe(cores=16, load=1.0, gb=5)}\nEOF\n;; esac''')
        self.free["/"] = 5.0
        drv, out = "/nix/store/aaa-x.drv", "/nix/store/bbb-x"
        argv = self.tmp / "argv"
        self.fake("nix", f"""
echo "$*" >> {argv}
case "$1" in
  build) echo '[{{"drvPath":"{drv}","outputs":{{"out":"{out}"}}}}]' ;;
  path-info) echo '{{"{drv}":null,"{out}":{{"references":[]}}}}' ;;
esac
""")
        store = storage.store_dir(self.ssd)
        orig_probe = build_hosts._probe
        async def fake_probe(cmd, timeout=15):
            if cmd[0] == "sh":
                return {"reachable": True, **build_hosts.parse_probe(probe(load=20))}
            return await orig_probe(cmd, timeout=timeout)
        with mock.patch.object(build_hosts, "_probe", fake_probe):
            res = asyncio.run(server.nix_build(mock.MagicMock(), drv, host="auto", store="auto", need_gb=0))
        store_url = build_hosts.remote_store("a@h1", store)
        self.assertIn(f"build --eval-store auto --store {store_url}", argv.read_text())
        self.assertIn(f"output: {out} (on a@h1: {store}{out})", res)
        self.assertIn(f"ssh a@h1 nix shell --store {store} {out} (binary unknown)", res)

    def test_nix_build_host_drive_store_output_formatting(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd],
            "reserveGb": 30,
            "largeGb": 5,
        }
        drv, out = "/nix/store/111-hello.drv", "/nix/store/222-hello"
        self.fake("nix", f"""
if [ "$1" = eval ]; then
  echo "/nix/store/222-hello/bin/hello"
  exit 0
fi
case "$1" in
  build) echo '[{{"drvPath":"{drv}","outputs":{{"out":"{out}"}}}}]' ;;
  path-info) echo '{{"{drv}":null,"{out}":{{"references":[]}}}}' ;;
esac
""")
        store = storage.store_dir(self.ssd)
        res = asyncio.run(server.nix_build(mock.MagicMock(), drv, host="a@h1", store=self.ssd))
        self.assertIn(f"output: {out} (on a@h1: {store}{out})", res)
        self.assertIn(f"ssh a@h1 nix shell --store {store} {out}", res)
        self.assertNotIn("on disk:", res)

    def test_build_cleanup_gc_includes_host_drive_stores(self) -> None:
        store = storage.store_dir(self.ssd)
        store_url = build_hosts.remote_store("a@h1", store)
        build_hosts._created[store_url] = {"/nix/store/xxx"}
        argv = self.tmp / "calls.log"
        self.fake("nix", f"""
echo "$*" >> {argv}
if [ "$1" = store ] && [ "$2" = gc ]; then
  echo "finding garbage collector roots..."
  echo "deleted 10 store paths"
fi
""")
        report = json.loads(asyncio.run(server.build_cleanup("a@h1", gc=True)))
        self.assertIn(f"gc {store_url}", report)
        self.assertIn(f"store gc --store {store_url}", argv.read_text())


    def test_build_cleanup_empty_target_gc_runs_on_fully_deleted_host_drive_stores(self) -> None:
        store = storage.store_dir(self.ssd)
        store_url = build_hosts.remote_store("a@h1", store)
        build_hosts._created[store_url] = {"/nix/store/xxx"}
        argv = self.tmp / "calls.log"
        self.fake("nix", f"""
echo "$*" >> {argv}
if [ "$1" = path-info ]; then
  echo '{{"/nix/store/xxx":{{"references":[]}}}}'
elif [ "$1" = store ] && [ "$2" = delete ]; then
  exit 0
elif [ "$1" = store ] && [ "$2" = gc ]; then
  echo "finding garbage collector roots..."
  echo "deleted 10 store paths"
fi
""")
        report = json.loads(asyncio.run(server.build_cleanup("", gc=True)))
        self.assertIn(f"gc {store_url}", report)
        self.assertIn(f"store gc --store {store_url}", argv.read_text())


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

    def test_remote_store_uses_host_drive(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd],
            "reserveGb": 30,
            "largeGb": 5,
        }
        calls = self.tmp / "calls"
        self.fake("nix", f'''
echo "$*" >> {calls}
case "$1" in
  path-info) echo '{{"/nix/store/y":{{"references":[]}}}}' ;;
  *) echo '[{{"drvPath":"/nix/store/x.drv","outputs":{{"out":"/nix/store/y"}}}}]' ;;
esac
''')
        out = asyncio.run(server.nix_build(mock.MagicMock(), "x#y", host="a@h1", store=self.ssd))
        store_path = storage.store_dir(self.ssd)
        self.assertIn(f"Build succeeded on a@h1 in {store_path}", out)
        store_url = build_hosts.remote_store("a@h1", store_path)
        self.assertIn(f"--store {store_url}", calls.read_text())
        self.assertIn(store_url, build_hosts._created)
        report = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertEqual(report[store_url], "deleted 1 paths")
        self.assertNotIn(store_url, build_hosts._created)

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




class PkgsHostTest(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.tmp / "calls.log"

    def test_run_package_bad_host(self) -> None:
        out = json.loads(asyncio.run(server.run_package("hello", host="evil@h9")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_pkgs_build_missing_file(self) -> None:
        out = json.loads(asyncio.run(server.pkgs_build("/nonexistent/package.nix", host="a@h1")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_run_package_on_host(self) -> None:
        self.fake("nix-instantiate", 'echo "/nix/store/111-hello.drv"')
        self.fake("nix", f"""
if [ "$1" = eval ]; then
  echo "/nix/store/222-hello/bin/hello"
  exit 0
fi
echo "$*" >> {self.log}
echo "copying path '/nix/store/222-hello' from 'x'..." >&2
echo '[{{"drvPath":"/nix/store/111-hello.drv","outputs":{{"out":"/nix/store/222-hello"}}}}] '
""")
        self.fake("ssh", f"""
echo "ssh $*" >> {self.log}
echo "hello (GNU Hello) 2.12"
""")
        out = asyncio.run(server.run_package("hello", args=["--version"], host="a@h1"))
        self.assertIn("hello (GNU Hello) 2.12", out)
        self.assertIn("/nix/store/222-hello", build_hosts._created["ssh-ng://a@h1"])
        log = self.log.read_text()
        self.assertIn("--store ssh-ng://a@h1", log)
        self.assertIn("/nix/store/222-hello/bin/hello --version", log)

    def test_run_package_on_drive_store(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd, "host"],
            "reserveGb": 30,
            "largeGb": 5,
        }
        self.fake("nix-instantiate", 'echo "/nix/store/111-hello.drv"')
        self.fake("nix", f"""
if [ "$1" = eval ]; then
  echo "/nix/store/222-hello/bin/hello"
  exit 0
fi
echo "$*" >> {self.log}
echo '[{{"drvPath":"/nix/store/111-hello.drv","outputs":{{"out":"/nix/store/222-hello"}}}}] '
""")
        self.fake("ssh", f"""
echo "ssh $*" >> {self.log}
echo "hello 2.12 on drive"
""")
        out = asyncio.run(server.run_package("hello", args=["--version"], host="a@h1"))
        self.assertIn("hello 2.12 on drive", out)
        store_path = f"{self.ssd}/ai-dump/nix-store"
        store_url = build_hosts.remote_store("a@h1", store_path)
        self.assertIn("/nix/store/222-hello", build_hosts._created[store_url])
        log = self.log.read_text()
        self.assertIn(f"--store {store_url}", log)
        self.assertIn(f"nix shell --store {store_path} /nix/store/222-hello --command hello --version", log)

    def test_pkgs_build_on_host(self) -> None:
        pkg = self.tmp / "package.nix"
        pkg.write_text("{}: {}")
        self.fake("nix-instantiate", 'echo "/nix/store/333-pkg.drv"')
        self.fake("nix", f"""
echo "$*" >> {self.log}
echo "copying path '/nix/store/444-pkg' from 'x'..." >&2
echo '[{{"drvPath":"/nix/store/333-pkg.drv","outputs":{{"out":"/nix/store/444-pkg"}}}}] '
""")
        self.fake("ssh", f"""
echo "ssh $*" >> {self.log}
echo "my-tool output ok"
""")
        with mock.patch("icedos_mcp.pkgs_host.find_build_pkg_expr", lambda: "/dummy/build-pkg.nix"):
            out = asyncio.run(server.pkgs_build(str(pkg), host="a@h1"))
            self.assertEqual(out, "/nix/store/444-pkg")
            self.assertIn("/nix/store/444-pkg", build_hosts._created["ssh-ng://a@h1"])

            out_run = asyncio.run(server.pkgs_build(str(pkg), run="my-tool", args=["--flag"], host="a@h1"))
            self.assertIn("my-tool output ok", out_run)
            self.assertIn("/nix/store/444-pkg/bin/my-tool --flag", self.log.read_text())

    def test_pkgs_build_on_drive_store(self) -> None:
        settings._hosts["a@h1"] = {
            "drives": {self.ssd: "random"},
            "priority": [self.ssd, "host"],
            "reserveGb": 30,
            "largeGb": 5,
        }
        pkg = self.tmp / "package.nix"
        pkg.write_text("{}: {}")
        self.fake("nix-instantiate", 'echo "/nix/store/333-pkg.drv"')
        self.fake("nix", f"""
echo "$*" >> {self.log}
echo '[{{"drvPath":"/nix/store/333-pkg.drv","outputs":{{"out":"/nix/store/444-pkg"}}}}] '
""")
        self.fake("ssh", f"""
echo "ssh $*" >> {self.log}
echo "my-tool on drive ok"
""")
        with mock.patch("icedos_mcp.pkgs_host.find_build_pkg_expr", lambda: "/dummy/build-pkg.nix"):
            out = asyncio.run(server.pkgs_build(str(pkg), run="my-tool", args=["--flag"], host="a@h1"))
            self.assertIn("my-tool on drive ok", out)
            store_path = f"{self.ssd}/ai-dump/nix-store"
            store_url = build_hosts.remote_store("a@h1", store_path)
            self.assertIn("/nix/store/444-pkg", build_hosts._created[store_url])
            log = self.log.read_text()
            self.assertIn(f"--store {store_url}", log)
            self.assertIn(f"nix shell --store {store_path} /nix/store/444-pkg --command my-tool --flag", log)

    def test_cleanup_removes_host_pkgs(self) -> None:
        build_hosts.record("ssh-ng://a@h1", "", ["/nix/store/999-pkg"])
        self.fake("nix", """
if [ "$1" = path-info ]; then
  echo '{"/nix/store/999-pkg":{"references":[]}}'
fi
if [ "$1" = store ] && [ "$2" = delete ]; then
  exit 0
fi
""")
        report = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertIn("ssh-ng://a@h1", report)
        self.assertNotIn("/nix/store/999-pkg", build_hosts._created.get("ssh-ng://a@h1", set()))

    def test_run_package_bad_attr(self) -> None:
        out = json.loads(asyncio.run(server.run_package("invalid;attr", host="a@h1")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_pkgs_build_bad_run(self) -> None:
        pkg = self.tmp / "package.nix"
        pkg.write_text("{}: {}")
        out1 = json.loads(asyncio.run(server.pkgs_build(str(pkg), run="../bad", host="a@h1")))
        self.assertEqual(out1["error"]["kind"], "bad_args")
        out2 = json.loads(asyncio.run(server.pkgs_build(str(pkg), run="sub/bad", host="a@h1")))
        self.assertEqual(out2["error"]["kind"], "bad_args")

    def test_host_already_had_output_cleanup_leaves_it(self) -> None:
        self.fake("nix-instantiate", 'echo "/nix/store/111-hello.drv"')
        self.fake("nix", f"""
if [ "$1" = eval ]; then
  echo "/nix/store/222-hello/bin/hello"
  exit 0
fi
echo "$*" >> {self.log}
echo '[{{"drvPath":"/nix/store/111-hello.drv","outputs":{{"out":"/nix/store/222-hello"}}}}] '
""")
        self.fake("ssh", f"""
echo "ssh $*" >> {self.log}
echo "hello (GNU Hello) 2.12"
""")
        out = asyncio.run(server.run_package("hello", args=["--version"], host="a@h1"))
        self.assertIn("hello (GNU Hello) 2.12", out)
        self.assertNotIn("/nix/store/222-hello", build_hosts._created.get("ssh-ng://a@h1", set()))
        report = json.loads(asyncio.run(server.build_cleanup("a@h1")))
        self.assertNotIn("/nix/store/222-hello", build_hosts._created.get("ssh-ng://a@h1", set()))

    def test_remote_timeout_kills_process_group(self) -> None:
        killed: list[tuple[str, str]] = []

        async def fake_kill(host: str, work: str) -> None:
            killed.append((host, work))

        with mock.patch("icedos_mcp.shell_run.kill_remote", fake_kill):
            with mock.patch("icedos_mcp.pkgs_host.run_streaming", side_effect=cli.CommandTimeout("timed out")):
                with self.assertRaises(cli.CommandTimeout):
                    asyncio.run(pkgs_host.run_remote_binary("a@h1", ["/bin/true"], timeout=1))

        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][0], "a@h1")
        self.assertIn("run-", killed[0][1])

if __name__ == "__main__":
    unittest.main()
