"""Run: nix-shell -p jq python3 --run "python3 -I test_seed.py" """
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SEED = Path(__file__).with_name("seed.sh")
SETTINGS = {"defaultModel": "m", "skills": ["/nix/store/x-pi-agent-skills"]}
MODELS = {"providers": {"llamacpp": {"baseUrl": "http://127.0.0.1:8080/v1"}}}


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dir = root / "agent"
        self.settings_seed = root / "settings-seed.json"
        self.models_seed = root / "models-seed.json"
        self.npm_log = root / "npm.log"
        self.mock_npm = root / "mock-npm"
        self.mock_npm.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{self.npm_log}"
""")
        self.mock_npm.chmod(0o755)
        self.settings_seed.write_text(json.dumps(SETTINGS))
        self.models_seed.write_text(json.dumps(MODELS))

    def tearDown(self):
        self.tmp.cleanup()

    def run_seed(self):
        return subprocess.run(
            ["bash", str(SEED), str(self.dir), str(self.settings_seed), str(self.models_seed), str(self.mock_npm)],
            check=True, capture_output=True, text=True
        )

    def read(self, name):
        return json.loads((self.dir / name).read_text())

    def test_fresh_dir_gets_seeds(self):
        self.run_seed()
        self.assertEqual(self.read("settings.json"), SETTINGS)
        self.assertEqual(self.read("models.json"), MODELS)

    def test_keeps_keys_pi_wrote(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text('{"theme": "dark", "defaultModel": "old"}')
        self.run_seed()
        self.assertEqual(self.read("settings.json"), {"theme": "dark", **SETTINGS})

    def test_corrupt_settings_moved_aside(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text("{not json")
        out = self.run_seed()
        self.assertEqual(self.read("settings.json"), SETTINGS)
        bad = list(self.dir.glob("settings.json.bad-*"))
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].read_text(), "{not json")
        self.assertIn("moved aside", out.stderr)

    def test_non_object_settings_moved_aside(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text("[]")
        self.run_seed()
        self.assertEqual(self.read("settings.json"), SETTINGS)
        self.assertEqual(len(list(self.dir.glob("settings.json.bad-*"))), 1)

    def test_models_replace_named_provider_keep_others(self):
        self.dir.mkdir()
        (self.dir / "models.json").write_text(json.dumps({"providers": {
            "llamacpp": {"baseUrl": "old", "models": [{"id": "gone"}]},
            "mine": {"baseUrl": "http://mine"},
        }}))
        self.run_seed()
        self.assertEqual(self.read("models.json")["providers"], {
            "llamacpp": {"baseUrl": "http://127.0.0.1:8080/v1"},
            "mine": {"baseUrl": "http://mine"},
        })

    def test_no_temp_files_left(self):
        self.run_seed()
        self.run_seed()
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            [".icedos-packages.json", ".icedos-settings-paths.json", "models.json", "settings.json"]
        )

    def write_settings_seed(self, data):
        self.settings_seed.write_text(json.dumps(data))

    def test_packages_seeded_and_recorded(self):
        self.write_settings_seed({**SETTINGS, "packages": ["npm:a@1"]})
        self.run_seed()
        self.assertEqual(self.read("settings.json")["packages"], ["npm:a@1"])
        self.assertEqual(self.read(".icedos-packages.json"), ["npm:a@1"])

    def test_packages_keep_pi_installs(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text('{"packages": ["npm:mine", "npm:a@1"]}')
        self.write_settings_seed({**SETTINGS, "packages": ["npm:a@1", "npm:b@2"]})
        self.run_seed()
        self.assertEqual(self.read("settings.json")["packages"], ["npm:mine", "npm:a@1", "npm:b@2"])

    def test_packages_drop_only_what_nix_added(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text('{"packages": ["npm:mine", "npm:old@1"]}')
        (self.dir / ".icedos-packages.json").write_text('["npm:old@1"]')
        self.run_seed()
        self.assertEqual(self.read("settings.json")["packages"], ["npm:mine"])
        self.assertEqual(self.read(".icedos-packages.json"), [])

    def test_no_packages_key_when_none(self):
        self.run_seed()
        self.assertNotIn("packages", self.read("settings.json"))

    def test_object_package_entries(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text('{"packages": [{"source": "npm:x", "skills": []}]}')
        self.run_seed()
        self.assertEqual(self.read("settings.json")["packages"], [{"source": "npm:x", "skills": []}])

    def test_nested_settings_dropped_on_removal_and_user_empty_obj_survives(self):
        self.dir.mkdir()
        (self.dir / "settings.json").write_text(json.dumps({
            "userCustom": "customValue",
            "userEmptyObj": {},
            "userParent": {"nestedEmpty": {}},
            "defaultModel": "gpt-4",
            "providers": {"openai": {"key": "k1", "url": "https://api.openai.com/v1"}},
            "compaction": {"reserve": 1000}
        }))
        # First run establishes ledger with full settings
        self.write_settings_seed({
            "defaultModel": "gpt-4",
            "providers": {"openai": {"key": "k1", "url": "https://api.openai.com/v1"}},
            "compaction": {"reserve": 1000}
        })
        self.run_seed()
        s1 = self.read("settings.json")
        self.assertEqual(s1["userCustom"], "customValue")
        self.assertEqual(s1["userEmptyObj"], {})
        self.assertEqual(s1["userParent"]["nestedEmpty"], {})
        self.assertEqual(s1["providers"]["openai"]["url"], "https://api.openai.com/v1")

        # Second run: url removed and compaction removed completely
        self.write_settings_seed({
            "defaultModel": "gpt-4",
            "providers": {"openai": {"key": "k1"}}
        })
        self.run_seed()
        s2 = self.read("settings.json")
        self.assertEqual(s2["userCustom"], "customValue")
        self.assertEqual(s2["userEmptyObj"], {})
        self.assertEqual(s2["userParent"]["nestedEmpty"], {})
        self.assertEqual(s2["providers"]["openai"], {"key": "k1"})
        self.assertNotIn("compaction", s2)

    def test_npm_package_uninstalled_when_removed(self):
        (self.dir / "npm").mkdir(parents=True)
        self.write_settings_seed({
            **SETTINGS,
            "packages": ["npm:@apothic-ai/pi-ghost-text-x@1.0.0", "/nix/store/local-pkg"]
        })
        self.run_seed()
        self.assertFalse(self.npm_log.exists())

        # Now remove ghost-text package
        self.write_settings_seed({
            **SETTINGS,
            "packages": ["/nix/store/local-pkg"]
        })
        self.run_seed()
        self.assertTrue(self.npm_log.exists())
        log = self.npm_log.read_text()
        self.assertIn("uninstall @apothic-ai/pi-ghost-text-x --prefix", log)
        self.assertIn("--offline", log)
        self.assertNotIn("local-pkg", log)


if __name__ == "__main__":
    unittest.main()
