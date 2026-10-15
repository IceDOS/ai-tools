"""Tests for sandboxes: copy, diff, delete, per-call root routing and --dir builds.

    PYTHONPATH=src python -m unittest tests.test_sandbox -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from icedos_mcp import cli, sandbox, server
from icedos_mcp.config import find_config_root, get_value, set_value


class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.live = tmp / "config"
        (self.live / "configs").mkdir(parents=True)
        (self.live / ".git").mkdir()
        (self.live / ".state" / ".cache" / "2026-01-01T00:00:00+00:00").mkdir(parents=True)
        (self.live / "flake.nix").write_text("icedos.lib.mkIceDOS {}\n")
        (self.live / "config.toml").write_text('[icedos.system]\narch = "x86_64-linux"\n')
        (self.live / "configs" / "extra.toml").write_text("[icedos.extra]\nx = 1\n")
        (self.live / ".state" / "flake.lock").write_text("{}\n")
        (self.live / ".state" / "build.sh").write_text(f'cd "{self.live}"\n')
        (self.live / ".state" / ".cache" / "options-doc.json").write_text("[]\n")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "icedos"
        # Fake icedos that echoes its argv, so the test sees how rebuild was called.
        exe.write_text(f'#!{shutil.which("bash")}\necho "argv: $*"\n')
        exe.chmod(0o755)
        self._old_env = {k: os.environ.get(k) for k in ("ICEDOS_CONFIG_DIR", "ICEDOS_SANDBOX_DIR", "PATH")}
        os.environ["ICEDOS_CONFIG_DIR"] = str(self.live)
        os.environ["ICEDOS_SANDBOX_DIR"] = str(tmp / "sandboxes")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self._old_mode = (server._live, server._default_name)
        server._live, server._default_name = False, None

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server._live, server._default_name = self._old_mode
        self._tmp.cleanup()

    def test_create_copies_config_but_not_git_or_live_state(self) -> None:
        meta = sandbox.create("a")
        root = Path(meta["path"])
        self.assertTrue((root / "configs" / "extra.toml").is_file())
        self.assertFalse((root / ".git").exists())
        self.assertTrue((root / ".state" / "flake.lock").is_file())
        self.assertTrue((root / ".state" / ".cache" / "options-doc.json").is_file())
        self.assertFalse((root / ".state" / ".cache" / "2026-01-01T00:00:00+00:00").exists())
        self.assertIn(f'cd "{root}"', (root / ".state" / "build.sh").read_text())

    def test_name_validation_and_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            sandbox.create("../escape")
        sandbox.create("dup")
        with self.assertRaises(FileExistsError):
            sandbox.create("dup")

    def test_writes_in_sandbox_leave_live_untouched(self) -> None:
        root = sandbox.resolve(sandbox.create("w")["name"])
        live_before = (self.live / "config.toml").read_text()
        from icedos_mcp.config import use_config_root

        with use_config_root(root):
            self.assertEqual(find_config_root(), root)
            set_value("icedos.system.arch", "aarch64-linux")
            self.assertEqual(get_value("icedos.system.arch"), "aarch64-linux")
        self.assertEqual(find_config_root(), self.live)
        self.assertEqual((self.live / "config.toml").read_text(), live_before)
        patch = sandbox.diff("w")
        self.assertIn('-arch = "x86_64-linux"', patch)
        self.assertIn('+arch = "aarch64-linux"', patch)
        self.assertNotIn(".state", patch)

    def test_diff_reports_added_files(self) -> None:
        root = sandbox.resolve(sandbox.create("n")["name"])
        (root / "configs" / "new.toml").write_text("a = 1")
        patch = sandbox.diff("n")
        self.assertIn("+++ b/configs/new.toml", patch)
        self.assertIn("\\ No newline at end of file", patch)

    def test_list_and_delete(self) -> None:
        sandbox.create("l")
        self.assertEqual([s["name"] for s in sandbox.list_all()], ["l"])
        sandbox.delete("l")
        self.assertEqual(sandbox.list_all(), [])
        with self.assertRaises(FileNotFoundError):
            sandbox.resolve("l")

    def test_tool_routes_to_sandbox_per_call(self) -> None:
        server._live = True
        sandbox.create("t")

        async def run() -> tuple[str, str]:
            return await asyncio.gather(
                server.get_config_value("icedos.system.arch"),
                server.set_config_value("icedos.system.arch", '"riscv64-linux"', validate=False, sandbox="t"),
            )

        live_read, _ = asyncio.run(run())
        self.assertEqual(json.loads(live_read)["value"], "x86_64-linux")
        sb_read = asyncio.run(server.get_config_value("icedos.system.arch", sandbox="t"))
        self.assertEqual(json.loads(sb_read)["value"], "riscv64-linux")

    def test_sandbox_mode_writes_default_sandbox_not_live(self) -> None:
        live_before = (self.live / "config.toml").read_text()
        out = asyncio.run(server.set_config_value("icedos.system.arch", '"aarch64-linux"', validate=False))
        self.assertIn("Set icedos.system.arch", out)
        self.assertEqual((self.live / "config.toml").read_text(), live_before)
        name = server._default_name
        assert name is not None
        read = json.loads(asyncio.run(server.get_config_value("icedos.system.arch")))
        self.assertEqual(read["value"], "aarch64-linux")
        self.assertIn('+arch = "aarch64-linux"', asyncio.run(server.get_diff()))
        listing = json.loads(asyncio.run(server.sandbox_list()))
        self.assertEqual((listing["mode"], listing["default"]), ("sandbox", name))

    def test_deleted_default_sandbox_is_recreated(self) -> None:
        asyncio.run(server.get_config())
        first = server._default_name
        assert first is not None
        asyncio.run(server.sandbox_delete(first))
        asyncio.run(server.get_config())
        self.assertNotEqual(server._default_name, first)
        self.assertTrue(sandbox.resolve(server._default_name or "").is_dir())

    def test_live_mode_skips_sandbox(self) -> None:
        server._live = True
        asyncio.run(server.set_config_value("icedos.system.arch", '"aarch64-linux"', validate=False))
        self.assertIn("aarch64-linux", (self.live / "config.toml").read_text())
        self.assertIsNone(server._default_name)

    def test_rollback_and_gc_need_live_mode(self) -> None:
        for out in (asyncio.run(server.rollback()), asyncio.run(server.gc())):
            self.assertEqual(json.loads(out)["error"]["kind"], "live_only")
        # dry runs only plan, so the fake icedos runs and echoes its argv
        self.assertIn("argv: configuration rollback --dry", asyncio.run(server.rollback(dry=True)))

    def test_unused_default_dropped_on_exit_changed_kept(self) -> None:
        asyncio.run(server.get_config())
        unused = server._default_name or ""
        server._drop_unused_default()
        self.assertFalse((sandbox.sandboxes_dir() / unused).exists())
        server._default_name = None
        asyncio.run(server.set_config_value("icedos.system.arch", '"aarch64-linux"', validate=False))
        kept = server._default_name or ""
        server._drop_unused_default()
        self.assertTrue((sandbox.sandboxes_dir() / kept).is_dir())

    def test_nested_config_files_load_and_write(self) -> None:
        nested = self.live / "configs" / "ai" / "agents"
        nested.mkdir(parents=True)
        (nested / "claude.toml").write_text('[[icedos.repositories]]\nurl = "github:icedos/ai-tools"\n')
        from icedos_mcp import config_files

        rels = [p.relative_to(self.live).as_posix() for p in config_files.list_config_files(self.live)]
        self.assertEqual(rels, ["config.toml", "configs/ai/agents/claude.toml", "configs/extra.toml"])
        self.assertEqual(config_files.find_repo_file(self.live, "github:icedos/ai-tools"), nested / "claude.toml")
        self.assertEqual(config_files.resolve_file(self.live, "configs/ai/new.toml"), self.live / "configs" / "ai" / "new.toml")

    def test_override_repo_without_touching_config_files(self) -> None:
        nested = self.live / "configs" / "ai"
        nested.mkdir()
        (nested / "tools.toml").write_text('[[icedos.repositories]]\nurl = "github:icedos/ai-tools"\nmodules = ["x"]\n')
        checkout = Path(self._tmp.name) / "ai-tools"
        checkout.mkdir()
        (checkout / "flake.nix").write_text("{}\n")
        from icedos_mcp import config_files

        out = json.loads(asyncio.run(server.sandbox_override_repo("ai-tools", str(checkout))))
        self.assertEqual(out["overrides"], {"github:icedos/ai-tools": f"path:{checkout.resolve()}"})
        root = sandbox.resolve(server._default_name or "")
        overlay = root / "configs" / config_files.OVERRIDES_FILE
        self.assertTrue(overlay.is_file())
        # core loads the overlay like any config file, before configs/ai/*
        files = config_files.list_config_files(root)
        self.assertLess(files.index(overlay), files.index(root / "configs" / "ai" / "tools.toml"))
        repos = config_files.merged_config(root)["icedos"]["repositories"]
        self.assertEqual(repos[0]["overrideUrl"], f"path:{checkout.resolve()}")
        # the copied user file is unchanged, the diff stays empty and writes never target the overlay
        self.assertNotIn("overrideUrl", (root / "configs" / "ai" / "tools.toml").read_text())
        self.assertEqual(sandbox.diff(server._default_name or ""), "")
        asyncio.run(server.enable_module("y", repo="github:icedos/ai-tools"))
        self.assertIn('"y"', (root / "configs" / "ai" / "tools.toml").read_text())
        self.assertNotIn("modules", overlay.read_text())
        listing = json.loads(asyncio.run(server.sandbox_list()))
        self.assertEqual(listing["sandboxes"][0]["overrides"], out["overrides"])
        cleared = json.loads(asyncio.run(server.sandbox_override_repo("github:icedos/ai-tools")))
        self.assertEqual(cleared["overrides"], {})
        self.assertFalse(overlay.exists())

    def test_override_rejects_bad_input_and_live_mode(self) -> None:
        bad = json.loads(asyncio.run(server.sandbox_override_repo("nope", "/tmp")))
        self.assertEqual(bad["error"]["kind"], "sandbox")
        server._live = True
        live = json.loads(asyncio.run(server.sandbox_override_repo("github:icedos/x", "/tmp")))
        self.assertIn("only apply inside a sandbox", live["error"]["message"])

    def test_unknown_sandbox_is_an_error(self) -> None:
        out = json.loads(asyncio.run(server.get_config(sandbox="nope")))
        self.assertEqual(out["error"]["kind"], "sandbox")

    def test_rebuild_uses_dir_inside_sandbox(self) -> None:
        root = sandbox.resolve(sandbox.create("r")["name"])
        from icedos_mcp.config import use_config_root

        _, live_out, _ = asyncio.run(cli.rebuild("--dry"))
        self.assertIn("argv: rebuild --dry", live_out)

        async def in_sandbox() -> tuple[int, str, str]:
            with use_config_root(root):
                return await cli.rebuild("--dry")

        _, sb_out, _ = asyncio.run(in_sandbox())
        self.assertIn(f"argv: rebuild --dir {root} --dry", sb_out)

    def test_diff_ignores_live_edits_after_create_and_flags_drift(self) -> None:
        root = sandbox.resolve(sandbox.create("d")["name"])
        (self.live / "configs" / "extra.toml").write_text("[icedos.extra]\nx = 2\n")
        self.assertEqual(sandbox.diff("d"), "")
        self.assertEqual(sandbox.live_drift("d"), ["configs/extra.toml"])
        (root / "config.toml").write_text("")
        self.assertIn("configs/extra.toml", asyncio.run(server.sandbox_diff("d")))
        sandbox.delete("d")
        self.assertEqual(list((sandbox.sandboxes_dir() / ".meta").iterdir()), [])

    def test_sandbox_dir_must_be_private(self) -> None:
        base = sandbox.sandboxes_dir()
        base.chmod(0o755)
        with self.assertRaises(PermissionError):
            sandbox.sandboxes_dir()
        base.chmod(0o700)
        sandbox.create("x.json")
        self.assertEqual([s["name"] for s in sandbox.list_all()], ["x.json"])

    def test_externally_deleted_default_errors_once(self) -> None:
        asyncio.run(server.get_config())
        sandbox.delete(server._default_name or "")
        out = json.loads(asyncio.run(server.get_config()))
        self.assertIn("deleted outside this server", out["error"]["message"])
        self.assertNotIn("error", asyncio.run(server.get_config()))

    def test_rebuild_flag_checks(self) -> None:
        def kind(flags: str, **kw) -> str:
            out = asyncio.run(server.rebuild(None, flags, **kw))  # type: ignore[arg-type]
            return json.loads(out)["error"]["message"] if out.startswith("{") else out

        self.assertIn("not allowed in a sandbox", kind("--boot"))
        self.assertIn("not allowed in a sandbox", kind("--update-hooks"))
        self.assertIn("unknown rebuild flag", kind("--frobnicate"))
        self.assertIn("needs a value", kind("--update-repos-select"))
        self.assertIn("cannot parse", kind('"unterminated'))
        self.assertIn("--dir", kind("--dir /etc"))
        out = kind("--update --lock-only", logs=True)
        self.assertIn("--update --lock-only --logs", out)
        self.assertNotIn("--build", out)
        server._live = True
        self.assertIn("argv: rebuild --boot", kind("--boot"))

    def test_state_path_check_uses_path_containment(self) -> None:
        server._live = True
        sibling = self.live / ".state-evil"
        sibling.mkdir()
        (sibling / "x").write_text("secret")
        out = json.loads(asyncio.run(server.read_state_file("../.state-evil/x")))
        self.assertEqual(out["error"]["kind"], "bad_path")

    def test_resources_follow_the_default_sandbox(self) -> None:
        asyncio.run(server.set_config_value("icedos.system.arch", '"aarch64-linux"', validate=False))
        self.assertIn("aarch64-linux", asyncio.run(server.resource_config()))
        self.assertNotIn("aarch64-linux", (self.live / "config.toml").read_text())

    def test_format_nix_refuses_live_config_in_sandbox_mode(self) -> None:
        out = json.loads(asyncio.run(server.format_nix(str(self.live))))
        self.assertEqual(out["error"]["kind"], "live_only")
        self.assertIn("argv: nixf", asyncio.run(server.format_nix(".")))

    def test_override_needs_a_configured_repo_and_warns_on_earlier_override(self) -> None:
        checkout = Path(self._tmp.name) / "apps"
        checkout.mkdir()
        (checkout / "flake.nix").write_text("{}\n")
        (self.live / "configs" / ".a.toml").write_text(
            '[[icedos.repositories]]\nurl = "github:icedos/apps/dev"\noverrideUrl = "path:/x"\n'
        )
        bad = json.loads(asyncio.run(server.sandbox_override_repo("github:icedos/other", str(checkout))))
        self.assertIn("not a configured repository", bad["error"]["message"])
        out = json.loads(asyncio.run(server.sandbox_override_repo("github:icedos/apps", str(checkout))))
        self.assertEqual(out["urls"], ["github:icedos/apps/dev"])
        self.assertIn("configs/.a.toml", out["warnings"][0])

    def test_schema_exposes_sandbox_param(self) -> None:
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertIn("sandbox", tools["rebuild"].inputSchema["properties"])
        self.assertNotIn("ctx", tools["rebuild"].inputSchema["properties"])
        self.assertIn("sandbox", tools["set_config_value"].inputSchema["properties"])
        self.assertNotIn("sandbox", tools["gc"].inputSchema["properties"])


if __name__ == "__main__":
    unittest.main()
