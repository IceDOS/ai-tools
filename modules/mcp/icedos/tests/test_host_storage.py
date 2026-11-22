"""Tests for a build host's own storage settings, drives and picks.

Fake ssh serves the settings file, probe lines and du output.

    PYTHONPATH=src python -m unittest tests.test_host_storage -v
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

from icedos_mcp import server, settings, storage

HOST = "knp@192.168.1.50"
SETTINGS_CMD = f"cat {settings.HOST_FILE}"


class HostStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "ssh.log"
        self.host_file = self.tmp / "host-settings.json"
        self.probe_file = self.tmp / "probe.txt"
        self.du_file = self.tmp / "du.txt"
        self.cfg = self.tmp / "local-settings.json"
        self.cfg.write_text(json.dumps({"build": {"hosts": [HOST]}}))
        self.exit_code = 0
        self.fake_ssh()
        self._env = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            settings.ENV: str(self.cfg),
        })
        self._env.start()
        settings._hosts.clear()

    def tearDown(self) -> None:
        settings._hosts.clear()
        self._env.stop()
        self._tmp.cleanup()

    def fake_ssh(self) -> None:
        exe = self.bin / "ssh"
        exe.write_text(f"""#!{shutil.which('bash')}
echo "$*" >> {self.log}
for a in "$@"; do last="$a"; done
case "$last" in
  "{SETTINGS_CMD}")
    if [ -f {self.host_file} ]; then
      cat {self.host_file}
    else
      echo "cat: {settings.HOST_FILE}: No such file or directory" >&2
      exit 1
    fi ;;
  *"df -Pk /nix/store"*) cat {self.probe_file} ;;
  *"du -sm"*)
    if [ -f {self.du_file} ]; then cat {self.du_file}; fi ;;
  mkdir*) ;;
  *) exit 1 ;;
esac
""")
        exe.chmod(0o755)

    def host_settings(self, drives: dict[str, str], priority: list[str] | None = None, reserve_gb: int = 30) -> None:
        settings._hosts.clear()
        self.host_file.write_text(json.dumps({
            "build": {"hosts": ["ignored@h9"]},
            "storage": {"drives": drives, "priority": priority or [], "reserveGb": reserve_gb},
        }))

    def probe(
        self,
        root_free: float = 500.0,
        drives: list[tuple[str, float | None, float | None, bool, str]] | None = None,
    ) -> None:
        """Probe output using real df -Pk format with @@ tags: (mount, free, total, has_store, dev)"""
        lines = [
            f"@@root 0 /dev/mapper/knp--pc--nixos-nix--store {int(1000 * storage._GB)} "
            f"{int((1000 - root_free) * storage._GB)} {int(root_free * storage._GB)} 50% /nix/store"
        ]
        for i, (m, free, total, has_store, dev) in enumerate(drives or []):
            mounted_on = m if free is not None else "/"
            free_kb = int(free * storage._GB) if free is not None else int(50 * storage._GB)
            tot_kb = int(total * storage._GB) if total is not None else int(100 * storage._GB)
            used_kb = tot_kb - free_kb
            st_flag = 1 if has_store else 0
            lines.append(f"@@{i} {st_flag} {dev} {tot_kb} {used_kb} {free_kb} 50% {mounted_on}")
        self.probe_file.write_text("\n".join(lines) + "\n")

    def ssh_calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_host_file_overlays_defaults_and_is_cached(self) -> None:
        self.host_settings({"/mnt/ssd": "random"}, ["/mnt/ssd"], 10)
        cfg = settings.storage(HOST)
        self.assertEqual(cfg["drives"], {"/mnt/ssd": "random"})
        self.assertEqual(cfg["priority"], ["/mnt/ssd"])
        self.assertEqual(cfg["reserveGb"], 10)
        self.assertEqual(cfg["largeGb"], 5)
        settings.storage(HOST)
        self.assertEqual(self.ssh_calls().count(SETTINGS_CMD), 1)

    def test_missing_file_keeps_host_store_only(self) -> None:
        self.probe()
        cfg = settings.storage(HOST)
        self.assertEqual((cfg["drives"], cfg["priority"]), ({}, []))
        self.assertEqual(storage.store_for(1, host=HOST), ("", "small build, host store has room"))
        with self.assertRaises(ValueError):
            storage.pick(5, host=HOST)
        self.assertEqual(asyncio.run(storage.status(HOST))["drives"], [])

    def test_broken_file_keeps_defaults_without_caching(self) -> None:
        self.host_file.write_text("{not json")
        self.assertEqual(settings.storage(HOST)["drives"], {})
        self.assertNotIn(HOST, settings._hosts)

    def test_unmounted_drive_is_skipped(self) -> None:
        self.host_settings({"/mnt/ssd": "random", "/mnt/hdd": "sequential"})
        self.probe(drives=[
            ("/mnt/ssd", 100.0, 200.0, False, "/dev/sdb1"),
            ("/mnt/hdd", None, None, False, "/dev/sda1"),
        ])
        rows = storage.drives(HOST)
        self.assertEqual([r["mounted"] for r in rows], [True, False])
        self.assertNotIn("free_gb", rows[1])
        picked = storage.pick(10, "sequential", host=HOST)
        self.assertEqual(picked["mount"], "/mnt/ssd")
        self.assertFalse(picked["matches_io"])

    def test_spaces_in_device_name_parsed_correctly(self) -> None:
        self.host_settings({"/mnt/ssd": "random"})
        self.probe(drives=[
            ("/mnt/ssd", 120.0, 500.0, False, "/dev/disk/by-label/Fast SSD"),
        ])
        rows = storage.drives(HOST)
        self.assertTrue(rows[0]["mounted"])
        self.assertEqual(rows[0]["free_gb"], 120.0)
        self.assertEqual(rows[0]["total_gb"], 500.0)

    def test_priority_orders_host_drives_with_trailing_slashes(self) -> None:
        self.host_settings({"/mnt/a/": "random", "/mnt/b/": "random"}, ["/mnt/b/", "host"])
        self.probe(drives=[
            ("/mnt/a", 100.0, 200.0, False, "/dev/sdb1"),
            ("/mnt/b", 100.0, 200.0, False, "/dev/sdc1"),
        ])
        self.assertEqual(storage.pick(10, host=HOST)["mount"], "/mnt/b")
        self.assertEqual(storage.store_for(1, host=HOST)[0], storage.store_dir("/mnt/b"))

    def test_pick_creates_project_dir_over_ssh(self) -> None:
        self.host_settings({"/mnt/ssd": "random"})
        self.probe(drives=[("/mnt/ssd", 100.0, 200.0, False, "/dev/sdb1")])
        r = storage.pick(10, "random", "proj", host=HOST)
        self.assertEqual(r["dir"], "/mnt/ssd/ai-dump/proj")
        self.assertIn("mkdir -p /mnt/ssd/ai-dump/proj", self.ssh_calls())

    def test_server_status_returns_ai_dump_and_handles_empty(self) -> None:
        self.host_settings({"/mnt/ssd": "random"}, reserve_gb=10)
        self.probe(root_free=50, drives=[("/mnt/ssd", 100.0, 200.0, True, "/dev/sdb1")])
        self.du_file.write_text("==/mnt/ssd\n25\t/mnt/ssd/ai-dump/proj1\n")
        r = json.loads(asyncio.run(server.storage_status(HOST)))
        self.assertEqual(r["reserve_gb"], 10)
        self.assertEqual(r["root"]["mount"], "/nix/store")
        self.assertEqual(r["root"]["free_gb"], 50.0)
        self.assertEqual(r["drives"][0]["ai_dump_mb"], {"proj1": 25})

    def test_server_refuses_unknown_host(self) -> None:
        out = json.loads(asyncio.run(server.storage_pick(1, host="evil@h3")))
        self.assertEqual(out["error"]["kind"], "bad_args")
        out = json.loads(asyncio.run(server.storage_status("evil@h3")))
        self.assertEqual(out["error"]["kind"], "bad_args")

    def test_probe_failure_is_reported(self) -> None:
        self.host_settings({"/mnt/ssd": "random"})
        with self.assertRaises(ValueError):
            storage.drives(HOST)
        out = json.loads(asyncio.run(server.storage_status(HOST)))
        self.assertEqual(out["error"]["kind"], "storage")

    def test_local_never_calls_ssh(self) -> None:
        self.cfg.write_text(json.dumps({"build": {"hosts": [HOST]}, "storage": {"reserveGb": 7}}))
        self.assertEqual(storage.settings()["reserveGb"], 7)
        self.assertEqual(self.ssh_calls(), "")



    def test_spaces_in_mount_path_parsed_correctly(self) -> None:
        self.host_settings({"/mnt/fast ssd": "random"})
        self.probe(drives=[
            ("/mnt/fast ssd", 150.0, 500.0, False, "/dev/sdb1"),
        ])
        rows = storage.drives(HOST)
        self.assertTrue(rows[0]["mounted"])
        self.assertEqual(rows[0]["mount"], "/mnt/fast ssd")
        self.assertEqual(rows[0]["free_gb"], 150.0)

    def test_store_for_auto_real_probe_checks_priority(self) -> None:
        # Drive comes before host in priority
        self.host_settings({"/mnt/ssd": "random"}, priority=["/mnt/ssd", "host"], reserve_gb=20)
        self.probe(root_free=100.0, drives=[("/mnt/ssd", 80.0, 200.0, False, "/dev/sdb1")])
        store, why = storage.store_for(1.0, "auto", host=HOST)
        self.assertEqual(store, storage.store_dir("/mnt/ssd"))
        self.assertIn("drive comes before the host store", why)

        # Host comes before drive in priority: small build fits host
        self.host_settings({"/mnt/ssd": "random"}, priority=["host", "/mnt/ssd"], reserve_gb=20)
        store, why = storage.store_for(1.0, "auto", host=HOST)
        self.assertEqual(store, "")
        self.assertIn("host store has room", why)

        # Large build (>= largeGb 5) picks SSD even if host comes first
        store, why = storage.store_for(10.0, "auto", host=HOST)
        self.assertEqual(store, storage.store_dir("/mnt/ssd"))
        self.assertIn("10 GB build", why)

if __name__ == "__main__":
    unittest.main()
