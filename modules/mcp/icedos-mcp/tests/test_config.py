"""Tests for multi-file config semantics: containment, strict merge, merged indices, repos and users.

    PYTHONPATH=src python -m unittest tests.test_config -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from icedos_mcp import config, config_files, server


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "config"
        (self.root / "configs" / "sub").mkdir(parents=True)
        (self.root / "flake.nix").write_text("{}\n")
        (self.root / "config.toml").write_text(
            '[icedos.system]\narch = "x86_64-linux"\npackages = ["a", "b"]\n\n'
            '[[icedos.repositories]]\nurl = "github:icedos/apps"\nmodules = ["steam"]\n'
        )
        (self.root / "configs" / "extra.toml").write_text(
            '[icedos.system]\npackages = ["c", "d"]\n\n'
            '[[icedos.repositories]]\nurl = "github:icedos/apps/dev"\nmodules = ["btop"]\n\n'
            '[icedos.users.ice]\nsudo = true\n'
        )
        self._old = (os.environ.get("ICEDOS_CONFIG_DIR"), server._live)
        os.environ["ICEDOS_CONFIG_DIR"] = str(self.root)
        server._live = True

    def tearDown(self) -> None:
        if self._old[0] is None:
            os.environ.pop("ICEDOS_CONFIG_DIR", None)
        else:
            os.environ["ICEDOS_CONFIG_DIR"] = self._old[0]
        server._live = self._old[1]
        self._tmp.cleanup()

    def read(self, rel: str) -> dict:
        return tomllib.loads((self.root / rel).read_text())

    def test_extra_configs_must_stay_inside_root(self) -> None:
        for bad in ('"../.."', '"/etc"', '"configs/../../x"'):
            (self.root / "config.toml").write_text(f"[icedos.system]\nextraConfigs = [{bad}]\n")
            with self.assertRaises(ValueError):
                config_files.list_config_files(self.root)
        with self.assertRaises(ValueError):
            config_files.resolve_file(self.root, "../outside.toml")

    def test_broken_toml_and_duplicate_scalars_raise(self) -> None:
        (self.root / "configs" / "dup.toml").write_text('[icedos.system]\narch = "aarch64-linux"\n')
        with self.assertRaisesRegex(ValueError, "duplicate config key 'icedos.system.arch'"):
            config_files.merged_config(self.root)
        (self.root / "configs" / "dup.toml").write_text("[icedos\n")
        with self.assertRaisesRegex(ValueError, "invalid TOML"):
            config_files.merged_config(self.root)

    def test_merged_index_maps_to_owning_file(self) -> None:
        self.assertEqual(config.get_value("icedos.system.packages[2]"), "c")
        config.set_value("icedos.system.packages[2]", "C")
        self.assertEqual(self.read("configs/extra.toml")["icedos"]["system"]["packages"], ["C", "d"])
        self.assertEqual(self.read("config.toml")["icedos"]["system"]["packages"], ["a", "b"])
        config.delete_value("icedos.system.packages[-1]")
        self.assertEqual(self.read("configs/extra.toml")["icedos"]["system"]["packages"], ["C"])
        config.set_value("icedos.system.packages[]", "e")
        self.assertEqual(config.get_value("icedos.system.packages"), ["a", "b", "C", "e"])

    def test_delete_by_value_searches_every_file(self) -> None:
        config.delete_value("icedos.system.packages|d")
        self.assertEqual(self.read("configs/extra.toml")["icedos"]["system"]["packages"], ["c"])
        with self.assertRaises(ValueError):
            config.delete_value("icedos.system.packages|zzz")

    def test_whole_merged_value_needs_a_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "merged from several files"):
            config.set_value("icedos.system.packages", ["x"])
        config.set_value("icedos.system.packages", ["x"], file="configs/extra.toml")
        self.assertEqual(config.get_value("icedos.system.packages"), ["a", "b", "x"])

    def test_new_key_goes_next_to_its_parent_and_quoted_keys_work(self) -> None:
        config.set_value("icedos.users.ice.home", "/home/ice")
        self.assertEqual(self.read("configs/extra.toml")["icedos"]["users"]["ice"]["home"], "/home/ice")
        config.set_value('environment.etc."foo.conf".text', "hi")
        self.assertEqual(self.read("config.toml")["environment"]["etc"]["foo.conf"]["text"], "hi")
        self.assertEqual(config.get_value("environment.etc.'foo.conf'.text"), "hi")

    def test_modules_match_by_base_url_across_entries(self) -> None:
        with self.assertRaisesRegex(ValueError, "already enabled"):
            config.set_module_enabled("btop", "github:icedos/apps", True)
        config.set_module_enabled("vlc", "github:icedos/apps/dev", True)
        self.assertIn("vlc", self.read("config.toml")["icedos"]["repositories"][0]["modules"])
        config.set_value("icedos.repositories[0].modules[]", "vlc", file="configs/extra.toml")
        written = config.set_module_enabled("vlc", "github:icedos/apps", False)
        self.assertEqual(len(written), 2)
        self.assertNotIn("vlc", json.dumps(config_files.merged_config(self.root)))

    def test_add_and_list_repositories(self) -> None:
        with self.assertRaisesRegex(ValueError, "already exists"):
            config.add_repository("github:icedos/apps/other")
        config.add_repository(
            "github:icedos/desktop",
            input_patches=[{"module": "kde", "input": "plasma", "patches": ["p.patch"]}],
        )
        added = self.read("config.toml")["icedos"]["repositories"][-1]
        self.assertEqual(added["input-patches"][0]["input"], "plasma")
        rows = {r["url"]: r for r in config.list_repositories()}
        self.assertEqual(rows["github:icedos/apps"]["modules"], ["steam", "btop"])
        self.assertEqual(rows["github:icedos/apps"]["files"], ["config.toml", "configs/extra.toml"])

    def test_edit_user_writes_each_key_to_its_owner(self) -> None:
        config.edit_user("ice", {"sudo": False, "description": "Ice"})
        user = self.read("configs/extra.toml")["icedos"]["users"]["ice"]
        self.assertEqual((user["sudo"], user["description"]), (False, "Ice"))
        config.edit_user("first.last", {"sudo": True})
        self.assertTrue(self.read("configs/extra.toml")["icedos"]["users"]["first.last"]["sudo"])

    def test_failed_write_reverts_every_file(self) -> None:
        before = {p: p.read_text() for p in config_files.all_config_tomls(self.root)}
        out = asyncio.run(server.set_config_value("icedos.system.arch", '"aarch64-linux"', file="configs/new.toml", validate=False))
        self.assertEqual(json.loads(out)["error"]["kind"], "write_error")
        self.assertFalse((self.root / "configs" / "new.toml").exists())
        # validation without .state/build.sh fails to run, which must also revert
        out = asyncio.run(server.set_config_value("icedos.system.foo", "1"))
        self.assertEqual(json.loads(out)["error"]["kind"], "validation")
        self.assertEqual({p: p.read_text() for p in config_files.all_config_tomls(self.root)}, before)

    def test_disabled_file_toggle_and_listing(self) -> None:
        out = asyncio.run(server.set_config_file_enabled("configs/extra.toml", False))
        self.assertIn("Disabled configs/extra.toml", out)
        rows = {r["file"]: r["loaded"] for r in json.loads(asyncio.run(server.list_config_files()))}
        self.assertEqual(rows, {"config.toml": True, "configs/extra.toml": False})
        note = asyncio.run(server.set_config_value("icedos.x", "1", file="configs/extra.toml", validate=False))
        self.assertIn("enable = false", note)
        asyncio.run(server.set_config_file_enabled("configs/extra.toml", True))
        self.assertNotIn("enable", self.read("configs/extra.toml"))
    def test_config_root_needs_mkicedos(self) -> None:
        repo = Path(self._tmp.name) / "repo"
        (repo / "modules").mkdir(parents=True)
        (repo / "flake.nix").write_text("{ outputs = _: { }; }\n")
        self.assertFalse(config._is_config_root(repo))
        (repo / "flake.nix").write_text("{ outputs = i: i.icedos.lib.mkIceDOS { }; }\n")
        self.assertTrue(config._is_config_root(repo))

    def test_every_prompt_renders(self) -> None:
        from icedos_mcp.prompts import PROMPTS, get_prompt

        for name in PROMPTS:
            self.assertTrue(get_prompt(name, module_name="m", module_url="u", issue="i", app_name="a"))


if __name__ == "__main__":
    unittest.main()
