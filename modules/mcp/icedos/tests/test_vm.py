"""Tests for VM build, lifecycle, TOML injection, and cleanup."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib

from icedos_mcp import config_files, leftovers, sandbox as sandboxes, server, vm
from tests.test_build_hosts import _EnvMixin


class TestVmUnit(_EnvMixin, unittest.TestCase):
    def test_find_free_port(self) -> None:
        port = vm.find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_pick_vm_user(self) -> None:
        merged = {
            "icedos": {
                "users": {
                    "sys": {"isSystemUser": True, "isNormalUser": False},
                    "alice": {"isNormalUser": True},
                    "bob": {"isNormalUser": True},
                }
            }
        }
        self.assertEqual(vm.pick_vm_user(merged), "alice")
        self.assertEqual(vm.pick_vm_user(merged, "bob"), "bob")
        with self.assertRaises(ValueError):
            vm.pick_vm_user(merged, "nonexistent")

        no_users = {"icedos": {"users": {}}}
        with self.assertRaises(ValueError):
            vm.pick_vm_user(no_users)

    def test_check_disk_size(self) -> None:
        self.assertEqual(vm.check_disk_size({}), (20.0, True))
        self.assertEqual(vm.check_disk_size({"icedos": {"system": {"build-vm": {"diskSize": "auto"}}}}), (20.0, True))
        self.assertEqual(vm.check_disk_size({"icedos": {"system": {"build-vm": {"diskSize": 0}}}}), (20.0, True))
        self.assertEqual(vm.check_disk_size({"icedos": {"system": {"build-vm": {"diskSize": 40960}}}}), (40.0, False))
        self.assertEqual(vm.check_disk_size({"icedos": {"system": {"build-vm": {"diskSize": 30}}}}), (30.0, False))

    def test_generate_vm_toml_ssh_true(self) -> None:
        toml_str = vm.generate_vm_toml(
            user="alice",
            password="secretpassword",
            ssh_pubkey="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...",
            ssh_port=2222,
            needs_disk_size=True,
            disk_size_mb=20480,
            ssh=True,
        )
        parsed = tomllib.loads(toml_str)
        vm_var = parsed["virtualisation"]["vmVariant"]
        self.assertEqual(vm_var["services"]["displayManager"]["autoLogin"]["user"], "alice")
        self.assertTrue(vm_var["services"]["displayManager"]["autoLogin"]["enable"])
        self.assertEqual(vm_var["services"]["getty"]["autologinUser"], "alice")
        self.assertEqual(vm_var["users"]["users"]["alice"]["password"], "secretpassword")
        self.assertEqual(
            vm_var["users"]["users"]["alice"]["openssh"]["authorizedKeys"]["keys"],
            ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI..."],
        )
        self.assertFalse(vm_var["services"]["openssh"]["settings"]["PasswordAuthentication"])
        self.assertFalse(vm_var["services"]["openssh"]["settings"]["KbdInteractiveAuthentication"])
        self.assertEqual(vm_var["services"]["openssh"]["settings"]["PermitRootLogin"], "no")
        self.assertEqual(
            vm_var["virtualisation"]["forwardPorts"],
            [{"from": "host", "host": {"address": "127.0.0.1", "port": 2222}, "guest": {"port": 22}}],
        )
        self.assertEqual(parsed["icedos"]["system"]["build-vm"]["diskSize"], 20480)

    def test_generate_vm_toml_ssh_false(self) -> None:
        toml_str = vm.generate_vm_toml(
            user="alice",
            password="secretpassword",
            needs_disk_size=False,
            ssh=False,
        )
        parsed = tomllib.loads(toml_str)
        vm_var = parsed["virtualisation"]["vmVariant"]
        self.assertEqual(vm_var["services"]["displayManager"]["autoLogin"]["user"], "alice")
        self.assertEqual(vm_var["services"]["getty"]["autologinUser"], "alice")
        self.assertEqual(vm_var["users"]["users"]["alice"]["password"], "secretpassword")
        self.assertNotIn("openssh", vm_var.get("services", {}))
        self.assertNotIn("virtualisation", vm_var)
        self.assertNotIn("openssh", vm_var["users"]["users"]["alice"])
        self.assertNotIn("icedos", parsed)

    def test_format_hint(self) -> None:
        hint_kwin = vm.format_hint(["kwin-mcp"], "/bin/run-vm", {"NIX_DISK_IMAGE": "test"}, 2222, "/key", "alice")
        self.assertIn("kwin-mcp is configured: session_start", hint_kwin)
        self.assertIn("app_launch", hint_kwin)
        self.assertIn("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 -i /key alice@127.0.0.1", hint_kwin)

        hint_kwin_no_ssh = vm.format_hint(["kwin-mcp"], "/bin/run-vm", {"NIX_DISK_IMAGE": "test"}, None, None, "alice")
        self.assertIn("kwin-mcp is configured: session_start", hint_kwin_no_ssh)
        self.assertNotIn("ssh", hint_kwin_no_ssh)

        hint_generic = vm.format_hint([], "/bin/run-vm", {"NIX_DISK_IMAGE": "test"}, 2222, "/key", "alice")
        self.assertIn("No desktop-control MCP detected", hint_generic)
        self.assertIn("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 -i /key alice@127.0.0.1", hint_generic)

        hint_generic_no_ssh = vm.format_hint([], "/bin/run-vm", {"NIX_DISK_IMAGE": "test"}, None, None, "alice")
        self.assertIn("No desktop-control MCP detected", hint_generic_no_ssh)
        self.assertNotIn("ssh", hint_generic_no_ssh)

    def test_sandbox_diff_skips_vm_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "config"
            configs = root / "configs"
            configs.mkdir(parents=True)
            (root / "flake.nix").write_text("icedos.lib.mkIceDOS {}\n")
            (configs / "base.toml").write_text("[icedos.users.alice]\nisNormalUser = true\n")

            old_cfg = os.environ.get("ICEDOS_CONFIG_DIR")
            old_sb = os.environ.get("ICEDOS_SANDBOX_DIR")
            try:
                os.environ["ICEDOS_CONFIG_DIR"] = str(root)
                os.environ["ICEDOS_SANDBOX_DIR"] = str(Path(td) / "sandboxes")

                meta = sandboxes.create("sb-vm-test")
                sb_dir = Path(meta["path"])
                vm_file = sb_dir / "configs" / config_files.VM_FILE
                vm_file.write_text("[virtualisation.vmVariant]\nservices.openssh.enable = true\n")

                tracked = sandboxes._tracked_files(sb_dir)
                self.assertNotIn(f"configs/{config_files.VM_FILE}", tracked)

                diff_out = sandboxes.diff("sb-vm-test")
                self.assertEqual(diff_out, "")
            finally:
                sandboxes.delete("sb-vm-test")
                if old_cfg is None:
                    os.environ.pop("ICEDOS_CONFIG_DIR", None)
                else:
                    os.environ["ICEDOS_CONFIG_DIR"] = old_cfg
                if old_sb is None:
                    os.environ.pop("ICEDOS_SANDBOX_DIR", None)
                else:
                    os.environ["ICEDOS_SANDBOX_DIR"] = old_sb

    def test_kill_vm_pid_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            disk_dir = Path(td) / "disk"
            disk_dir.mkdir()
            pidfile = disk_dir / "qemu.pid"
            pidfile.write_text("12345")

            with mock.patch("icedos_mcp.vm._is_qemu_for_disk", return_value=False):
                with mock.patch("os.kill") as mock_kill:
                    killed = vm.kill_vm_pid(pidfile, str(disk_dir))
                    self.assertIsNone(killed)
                    mock_kill.assert_not_called()

            pidfile.write_text("12345")
            with mock.patch("icedos_mcp.vm._is_qemu_for_disk", return_value=True):
                with mock.patch("os.kill") as mock_kill:
                    mock_kill.side_effect = [None, OSError(3, "No such process")]
                    killed = vm.kill_vm_pid(pidfile, str(disk_dir))
                    self.assertEqual(killed, 12345)
                    self.assertTrue(mock_kill.called)

    def test_cleanup_vm_removes_build_dir_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            build_dir = td_path / "build"
            build_dir.mkdir()
            result_link = build_dir / "result"
            result_link.symlink_to("/nix/store/dummy-vm")

            disk_dir = td_path / "disk"
            disk_dir.mkdir()

            vm.record_vm("sb-root-test", disk_dir, td_path, build_dir=build_dir)
            with mock.patch.object(leftovers, "remove") as mock_remove:
                res = vm.cleanup_vm("sb-root-test", root=td_path)
                mock_remove.assert_called_once_with([str(result_link)], delete=True)
                self.assertEqual(res["removed_root"], str(result_link))
                self.assertFalse(build_dir.exists())

    def test_cleanup_vm_server_restart_fallback(self) -> None:
        res = vm.cleanup_vm("nonexistent-sandbox-12345")
        self.assertEqual(res["sandbox"], "nonexistent-sandbox-12345")
        self.assertIsNone(res["killed_pid"])
        self.assertIsNone(res["deleted_disk_dir"])
        self.assertFalse(res["cleaned_overrides"])
        self.assertEqual(res["removed_root"], "no build record (skipped root cleanup)")

    def test_find_vm_script_returns_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store_dir = td_path / "nix" / "store" / "test-vm-store"
            (store_dir / "bin").mkdir(parents=True)
            script = store_dir / "bin" / "run-test-vm"
            script.write_text("#!/bin/sh\n")

            build_dir = td_path / "tmp" / "icedos-build-123"
            build_dir.mkdir(parents=True)
            (build_dir / "result").symlink_to(store_dir)

            found = vm.find_vm_script(build_dir, store_dir)
            self.assertEqual(found, script)
            self.assertFalse(str(found).startswith(str(build_dir)))

    def test_vm_record_persisted_to_disk_and_recovered_on_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            disk_dir = td_path / "disk"
            disk_dir.mkdir()
            build_dir = td_path / "build"
            build_dir.mkdir()
            result_link = build_dir / "result"
            result_link.symlink_to("/nix/store/test-vm")

            # Persist record
            vm.record_vm("sb-persist-test", disk_dir, td_path, build_dir=build_dir, ssh_port=2222, user="alice")
            json_file = disk_dir / "vm.json"
            self.assertTrue(json_file.is_file())
            data = json.loads(json_file.read_text())
            self.assertEqual(data["build_dir"], str(build_dir))
            self.assertEqual(data["ssh_port"], 2222)
            self.assertEqual(data["user"], "alice")

            # Clear in-memory record to simulate server restart
            vm._active_vms.pop("sb-persist-test", None)

            # Cleanup should read vm.json, recover build_dir, and unlink GC root
            with mock.patch("icedos_mcp.storage.drives", return_value=[{"mount": str(td_path)}]):
                with mock.patch("icedos_mcp.storage.AI_DUMP", "disk"):
                    # create symlink or candidate matching storage.AI_DUMP / f"vm-{name}"
                    candidate = td_path / "disk" / "vm-sb-persist-test"
                    candidate.mkdir(parents=True)
                    (candidate / "vm.json").write_text(json.dumps({
                        "sandbox": "sb-persist-test",
                        "root": str(td_path),
                        "build_dir": str(build_dir),
                        "ssh_port": 2222,
                        "user": "alice",
                    }))
                    with mock.patch.object(leftovers, "remove") as mock_remove:
                        res = vm.cleanup_vm("sb-persist-test")
                        mock_remove.assert_called_once_with([str(result_link)], delete=True)
                        self.assertEqual(res["removed_root"], str(result_link))

    def test_vm_build_tool_end_to_end(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            root = td_path / "config"
            configs = root / "configs"
            configs.mkdir(parents=True)
            (root / "flake.nix").write_text("icedos.lib.mkIceDOS {}\n")
            (configs / "base.toml").write_text("[icedos.users.alice]\nisNormalUser = true\n")

            store_dir = td_path / "nix" / "store" / "test-system-vm"
            (store_dir / "bin").mkdir(parents=True)
            vm_script = store_dir / "bin" / "run-icedos-vm"
            vm_script.write_text("#!/bin/sh\n")

            build_dir = td_path / "build" / "icedos-build-999"
            build_dir.mkdir(parents=True)
            (build_dir / "result").symlink_to(store_dir)

            disk_dir = td_path / "disk" / "vm-sb-tool-test"
            disk_dir.mkdir(parents=True)

            old_cfg = os.environ.get("ICEDOS_CONFIG_DIR")
            old_sb = os.environ.get("ICEDOS_SANDBOX_DIR")
            try:
                os.environ["ICEDOS_CONFIG_DIR"] = str(root)
                os.environ["ICEDOS_SANDBOX_DIR"] = str(td_path / "sandboxes")

                _ = sandboxes.create("sb-tool-test")
                mock_ctx = mock.MagicMock()

                stdout_mock = f"building from path {build_dir}...\n"
                mock_pick = {
                    "dir": str(disk_dir),
                    "mount": "/mnt/games-hdd",
                    "io": "sequential",
                    "matches_io": False,
                    "free_gb": 100.0,
                    "room_gb": 70.0,
                }

                with mock.patch("icedos_mcp.storage.pick", return_value=mock_pick):
                    with mock.patch("icedos_mcp.server.cli_rebuild", new=mock.AsyncMock(return_value=(0, stdout_mock, ""))):
                        res_json = asyncio.run(server.vm_build(mock_ctx, sandbox="sb-tool-test"))
                        res = json.loads(res_json)

                        self.assertEqual(res["sandbox"], "sb-tool-test")
                        self.assertEqual(res["script"], str(vm_script))
                        self.assertEqual(res["launch"]["argv"], [str(vm_script)])
                        self.assertEqual(res["launch"]["cwd"], str(disk_dir))
                        self.assertIn("NIX_DISK_IMAGE", res["launch"]["env"])
                        self.assertIn("QEMU_OPTS", res["launch"]["env"])
                        self.assertEqual(res["login"]["user"], "alice")
                        self.assertIsNotNone(res["ssh"]["port"])
                        self.assertEqual(res["ssh"]["user"], "alice")
                        self.assertIn("warning", res)
                        self.assertIn("sequential drive", res["warning"])
                        self.assertTrue((disk_dir / "vm.json").is_file())
            finally:
                sandboxes.delete("sb-tool-test")
                if old_cfg is None:
                    os.environ.pop("ICEDOS_CONFIG_DIR", None)
                else:
                    os.environ["ICEDOS_CONFIG_DIR"] = old_cfg
                if old_sb is None:
                    os.environ.pop("ICEDOS_SANDBOX_DIR", None)
                else:
                    os.environ["ICEDOS_SANDBOX_DIR"] = old_sb
