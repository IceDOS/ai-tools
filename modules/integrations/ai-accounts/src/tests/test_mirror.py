import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import mirror


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.real, self.acct = root / "real", root / "acct"
        self.real.mkdir()
        (self.real / "settings.json").write_text("{}")
        (self.real / "projects").mkdir()
        (self.real / ".credentials.json").write_text("REAL")

    def tearDown(self):
        self.tmp.cleanup()

    def test_links_everything_but_private(self):
        mirror.sync(self.real, self.acct, {".credentials.json"})
        self.assertEqual(os.readlink(self.acct / "settings.json"), str(self.real / "settings.json"))
        self.assertTrue((self.acct / "projects").is_symlink())
        self.assertFalse((self.acct / ".credentials.json").exists())

    def test_private_file_left_alone(self):
        self.acct.mkdir()
        (self.acct / ".credentials.json").write_text("MINE")
        mirror.sync(self.real, self.acct, {".credentials.json"})
        self.assertEqual((self.acct / ".credentials.json").read_text(), "MINE")

    def test_prunes_dangling_links(self):
        mirror.sync(self.real, self.acct)
        (self.real / "settings.json").unlink()
        mirror.sync(self.real, self.acct)
        self.assertFalse(os.path.lexists(self.acct / "settings.json"))

    def test_skip_is_not_linked(self):
        mirror.sync(self.real, self.acct, skip={"projects"})
        self.assertFalse(os.path.lexists(self.acct / "projects"))

    def test_conflict_raises_and_keeps_file(self):
        self.acct.mkdir()
        (self.acct / "settings.json").write_text("LOCAL")
        with self.assertRaises(mirror.MirrorConflict) as cm:
            mirror.sync(self.real, self.acct)
        self.assertEqual(cm.exception.path, self.acct / "settings.json")
        self.assertEqual((self.acct / "settings.json").read_text(), "LOCAL")

    def test_nested_private_makes_real_dir(self):
        (self.real / "cli").mkdir()
        (self.real / "cli" / "token").write_text("REAL")
        (self.real / "cli" / "history").write_text("h")
        mirror.sync(self.real, self.acct, {"cli/token"})
        self.assertFalse((self.acct / "cli").is_symlink())
        self.assertTrue((self.acct / "cli" / "history").is_symlink())
        self.assertFalse((self.acct / "cli" / "token").exists())

    def test_nested_private_replaces_old_symlink(self):
        (self.real / "cli").mkdir()
        mirror.sync(self.real, self.acct)
        self.assertTrue((self.acct / "cli").is_symlink())
        mirror.sync(self.real, self.acct, {"cli/token"})
        self.assertFalse((self.acct / "cli").is_symlink())

    def test_check_reports_without_changes(self):
        self.acct.mkdir()
        (self.acct / "settings.json").write_text("LOCAL")
        problems = mirror.check(self.real, self.acct)
        self.assertEqual(len(problems), 1)
        self.assertIn("settings.json", problems[0])
        self.assertFalse(os.path.lexists(self.acct / "projects"))

    def test_lock_is_reentrant_across_calls(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}):
            with mirror.locked("claude-work"):
                pass
            with mirror.locked("claude-work"):
                pass

    def test_seed_copied_once_then_owned(self):
        mirror.sync(self.real, self.acct, seed={"settings.json"})
        dst = self.acct / "settings.json"
        self.assertFalse(dst.is_symlink())
        self.assertEqual(dst.read_text(), "{}")
        dst.write_text('{"mine": 1}')
        (self.real / "settings.json").write_text('{"real": 2}')
        mirror.sync(self.real, self.acct, seed={"settings.json"})
        self.assertEqual(dst.read_text(), '{"mine": 1}')

    def test_seed_replaces_old_symlink(self):
        mirror.sync(self.real, self.acct)
        mirror.sync(self.real, self.acct, seed={"settings.json"})
        self.assertFalse((self.acct / "settings.json").is_symlink())

    def test_seed_dir_copy_skips_private(self):
        cache = self.real / "cli" / "cache"
        cache.mkdir(parents=True)
        (cache / "meta.json").write_text("m")
        (cache / "onboarding.json").write_text("REAL")
        mirror.sync(self.real, self.acct, private={"cli/cache/onboarding.json"}, seed={"cli/cache"})
        self.assertEqual((self.acct / "cli/cache/meta.json").read_text(), "m")
        self.assertFalse((self.acct / "cli/cache/meta.json").is_symlink())
        self.assertFalse((self.acct / "cli/cache/onboarding.json").exists())

    def test_existing_seed_dir_loses_links_into_real(self):
        cache = self.real / "cli" / "cache"
        cache.mkdir(parents=True)
        (cache / "meta.json").write_text("m")
        (cache / "onboarding.json").write_text("REAL")
        mirror.sync(self.real, self.acct, private={"cli/cache/onboarding.json"})
        self.assertTrue((self.acct / "cli/cache/meta.json").is_symlink())
        mirror.sync(self.real, self.acct, private={"cli/cache/onboarding.json"}, seed={"cli/cache"})
        self.assertFalse((self.acct / "cli/cache/meta.json").is_symlink())
        self.assertEqual((self.acct / "cli/cache/meta.json").read_text(), "m")

    def test_identical_real_file_is_relinked(self):
        self.acct.mkdir()
        (self.acct / "settings.json").write_text("{}")
        mirror.sync(self.real, self.acct)
        self.assertTrue((self.acct / "settings.json").is_symlink())

    def test_check_flags_seed_left_as_symlink(self):
        mirror.sync(self.real, self.acct)
        problems = mirror.check(self.real, self.acct, seed={"settings.json"})
        self.assertEqual(len(problems), 1)
        self.assertIn("own copy", problems[0])
