"""The launcher's symlink farm: the real home minus .gemini, plus .gemini at the data dir."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agy_home


class FarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".gitconfig").write_text("")
        env = {"XDG_RUNTIME_DIR": str(Path(self.tmp.name) / "run")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for k in ("XDG_DATA_HOME", "XDG_STATE_HOME"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_farm_links_home_and_data(self):
        farm = agy_home.prepare(self.home)
        self.assertEqual(farm, self.home / ".local/state/icedos/antigravity/home")
        self.assertEqual(os.readlink(farm / ".ssh"), str(self.home / ".ssh"))
        self.assertEqual(os.readlink(farm / ".gemini"), str(self.home / ".local/share/gemini"))
        self.assertTrue((self.home / ".local/share/gemini").is_dir())
        self.assertFalse(os.path.lexists(self.home / ".gemini"))

    def test_resync_drops_removed_entries_and_keeps_gemini(self):
        farm = agy_home.prepare(self.home)
        (self.home / ".gitconfig").unlink()
        (self.home / "new").mkdir()
        agy_home.prepare(self.home)
        self.assertFalse(os.path.lexists(farm / ".gitconfig"))
        self.assertTrue((farm / "new").is_symlink())
        self.assertEqual(os.readlink(farm / ".gemini"), str(self.home / ".local/share/gemini"))

    def test_old_layout_link_still_uses_farm(self):
        (self.home / ".local/share/gemini").mkdir(parents=True)
        (self.home / ".gemini").symlink_to(self.home / ".local/share/gemini")
        self.assertTrue(agy_home.needs_farm(self.home))
        farm = agy_home.prepare(self.home)
        self.assertEqual(os.readlink(farm / ".gemini"), str(self.home / ".local/share/gemini"))

    def test_nested_agy_in_farm_is_left_alone(self):
        farm = agy_home.prepare(self.home)
        self.assertFalse(agy_home.needs_farm(farm))

    def test_home_with_gemini_is_left_alone(self):
        (self.home / ".gemini").mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch.object(agy_home.os, "execv") as execv, \
                mock.patch.object(agy_home.sys, "argv", ["agy_home.py", "/x/agy", "-p", "hi"]):
            agy_home.main()
            self.assertEqual(os.environ["HOME"], str(self.home))
        execv.assert_called_once_with("/x/agy", ["/x/agy", "-p", "hi"])
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
