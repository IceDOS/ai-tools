import json
import os
import tempfile
import unittest
from pathlib import Path

from ai_accounts import settings


class MergeTest(unittest.TestCase):
    def test_dicts_merge_lists_replace(self):
        got = settings.merge({"a": {"x": 1, "l": [1, 2]}, "b": 1}, {"a": {"y": 2, "l": [3]}})
        self.assertEqual(got, {"a": {"x": 1, "y": 2, "l": [3]}, "b": 1})

    def test_flatten(self):
        self.assertEqual(settings.flatten({"a": {"b": 1, "c": None}, "d": []}),
                         {("a", "b"): 1, ("a", "c"): None, ("d",): []})


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.file, self.ledger = root / "settings.json", root / ".ai-account-settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.file.write_text(json.dumps(data))

    def read(self):
        return json.loads(self.file.read_text())

    def test_sets_keys_and_keeps_agent_keys(self):
        self.write({"theme": "dark", "statusLine": {"command": "old"}})
        self.assertTrue(settings.apply(self.file, {"statusLine": {"command": "new"}}, self.ledger))
        self.assertEqual(self.read(), {"theme": "dark", "statusLine": {"command": "new"}})

    def test_none_deletes(self):
        self.write({"statusLine": {"command": "x"}, "theme": "dark"})
        settings.apply(self.file, {"statusLine": None}, self.ledger)
        self.assertEqual(self.read(), {"theme": "dark"})

    def test_unchanged_skips_write(self):
        self.write({"model": "opus"})
        before = self.file.stat().st_mtime_ns
        os.utime(self.file, ns=(before - 10**9, before - 10**9))
        self.assertFalse(settings.apply(self.file, {"model": "opus"}, self.ledger))
        self.assertEqual(self.file.stat().st_mtime_ns, before - 10**9)

    def test_removed_override_restores_old_value(self):
        self.write({"model": "sonnet"})
        settings.apply(self.file, {"model": "opus"}, self.ledger)
        settings.apply(self.file, {}, self.ledger)
        self.assertEqual(self.read(), {"model": "sonnet"})

    def test_removed_override_drops_key_that_was_absent(self):
        self.write({})
        settings.apply(self.file, {"model": "opus"}, self.ledger)
        settings.apply(self.file, {}, self.ledger)
        self.assertEqual(self.read(), {})

    def test_agent_change_survives_removal(self):
        self.write({"model": "sonnet"})
        settings.apply(self.file, {"model": "opus"}, self.ledger)
        self.write({"model": "haiku"})
        settings.apply(self.file, {}, self.ledger)
        self.assertEqual(self.read(), {"model": "haiku"})

    def test_missing_file_is_created(self):
        settings.apply(self.file, {"statusLine": {"command": "x"}}, self.ledger)
        self.assertEqual(self.read(), {"statusLine": {"command": "x"}})

    def test_keeps_file_mode(self):
        self.write({})
        os.chmod(self.file, 0o600)
        settings.apply(self.file, {"a": 1}, self.ledger)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_invalid_json_left_alone(self):
        self.file.write_text("{not json")
        self.assertFalse(settings.apply(self.file, {"a": 1}, self.ledger))
        self.assertEqual(self.file.read_text(), "{not json")
