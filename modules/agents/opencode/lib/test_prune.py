"""Run: nix-shell -p jq python3 --run "python3 -I test_prune.py" """
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

PRUNE = Path(__file__).with_name("prune-npm.sh")


class PruneNpmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_dir = root / "config"
        self.cache_dir = root / "cache"
        self.npm_cache = self.cache_dir / "npm"
        self.manifest_file = root / "manifest.json"
        self.config_dir.mkdir(parents=True)
        self.npm_cache.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_prune(self, specs):
        self.manifest_file.write_text(json.dumps(specs))
        return subprocess.run(
            ["bash", str(PRUNE), str(self.config_dir), str(self.cache_dir), str(self.manifest_file)],
            check=True, capture_output=True, text=True
        )

    def test_first_run_no_ledger_deletes_nothing(self):
        (self.npm_cache / "opencode-clm@latest").mkdir()
        (self.npm_cache / "user-plugin@1.0.0").mkdir()
        self.run_prune(["opencode-clm"])
        self.assertTrue((self.npm_cache / "opencode-clm@latest").exists())
        self.assertTrue((self.npm_cache / "user-plugin@1.0.0").exists())
        self.assertTrue((self.config_dir / ".icedos-npm-plugins.json").exists())

    def test_removed_plugin_pruned_and_user_plugin_survives(self):
        (self.npm_cache / "opencode-clm@latest").mkdir()
        (self.npm_cache / "opencode-ghost@latest").mkdir()
        (self.npm_cache / "user-plugin@1.0.0").mkdir()
        self.run_prune(["opencode-clm", "opencode-ghost"])

        self.run_prune(["opencode-clm"])
        self.assertTrue((self.npm_cache / "opencode-clm@latest").exists())
        self.assertTrue((self.npm_cache / "user-plugin@1.0.0").exists())
        self.assertFalse((self.npm_cache / "opencode-ghost@latest").exists())

    def test_version_change_removes_old_version(self):
        (self.npm_cache / "opencode-clm@latest").mkdir()
        self.run_prune(["opencode-clm"])

        (self.npm_cache / "opencode-clm@0.5.1").mkdir()
        self.run_prune(["opencode-clm@0.5.1"])
        self.assertFalse((self.npm_cache / "opencode-clm@latest").exists())
        self.assertTrue((self.npm_cache / "opencode-clm@0.5.1").exists())

    def test_scoped_package_and_parent_dir_cleanup(self):
        scoped_dir = self.npm_cache / "@log0u7" / "opencode-watt@latest"
        scoped_dir.mkdir(parents=True)
        self.run_prune(["@log0u7/opencode-watt"])
        self.assertTrue(scoped_dir.exists())

        self.run_prune([])
        self.assertFalse(scoped_dir.exists())
        self.assertFalse((self.npm_cache / "@log0u7").exists())

    def test_directory_traversal_spec_guarded(self):
        # A file outside npm_cache must never be touched
        outside_victim = self.cache_dir / "victim_file"
        outside_victim.write_text("safe")

        # Initial run with malicious/traversal specs
        self.run_prune(["../victim_file", "/etc/passwd", "..", ""])

        # Second run: removing them must not delete anything outside
        self.run_prune([])
        self.assertTrue(outside_victim.exists())
        self.assertEqual(outside_victim.read_text(), "safe")


if __name__ == "__main__":
    unittest.main()
