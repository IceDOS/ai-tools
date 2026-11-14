import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import statusline


class TestStatuslineQuota(unittest.TestCase):
    def test_merge_converts_reset_ts_dynamically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_file = tmp_path / "quota_cache.json"
            base_time = 1_800_000_000.0
            reset_ts = base_time + 3600.0
            cache_file.write_text(json.dumps({
                "ts": base_time,
                "quota": {
                    "gemini-5h": {
                        "remaining_fraction": 0.5,
                        "reset_time": "2027-01-15T09:00:00Z",
                        "reset_ts": reset_ts,
                        "reset_in_seconds": 3600.0,
                    }
                }
            }))
            payload = {}
            with mock.patch.object(statusline, "QUOTA_CACHE", cache_file), \
                 mock.patch("time.time", return_value=base_time + 40.0):
                statusline.maybe_refresh_quota(payload)

            q = payload["quota"]["gemini-5h"]
            self.assertEqual(q["remaining_fraction"], 0.5)
            self.assertEqual(q["reset_time"], "2027-01-15T09:00:00Z")
            self.assertAlmostEqual(q["reset_in_seconds"], 3560.0)

    def test_fetch_quota_records_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_file = tmp_path / "quota_cache.json"
            lock_file = tmp_path / "quota_fetch.lock"
            fake_quota_out = json.dumps({
                "command": {
                    "data": {
                        "groups": [{
                            "buckets": [{
                                "id": "gemini-5h",
                                "remaining_fraction": 0.75,
                                "reset_time": "2027-01-15T09:00:00Z",
                            }]
                        }]
                    }
                }
            })
            fake_proc = mock.Mock(returncode=0, stdout=fake_quota_out)
            recorded = []

            def fake_run(cmd, *args, **kwargs):
                if cmd[0] == "mock_agy":
                    return fake_proc
                if len(cmd) >= 2 and cmd[1] == "record":
                    recorded.append(json.loads(kwargs.get("input", "{}")))
                    return mock.Mock(returncode=0)
                return mock.Mock(returncode=0)

            with mock.patch.object(statusline, "CACHE_DIR", tmp_path), \
                 mock.patch.object(statusline, "QUOTA_CACHE", cache_file), \
                 mock.patch.object(statusline, "QUOTA_LOCK", lock_file), \
                 mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch("time.time", return_value=1_800_000_000.0):
                code = statusline.fetch_quota(agy_bin="mock_agy")
                self.assertEqual(code, 0)

            cached = json.loads(cache_file.read_text())
            bucket = cached["quota"]["gemini-5h"]
            self.assertEqual(bucket["remaining_fraction"], 0.75)
            self.assertEqual(bucket["reset_time"], "2027-01-15T09:00:00Z")
            self.assertIn("reset_ts", bucket)
            self.assertTrue(len(recorded) > 0)


if __name__ == "__main__":
    unittest.main()
