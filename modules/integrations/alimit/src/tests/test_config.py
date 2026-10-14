import unittest

from alimit import config


class TestWindowClassification(unittest.TestCase):
    def test_is_known_window(self):
        for w in config.WINDOW_ORDER:
            self.assertTrue(config.is_known_window(w))
        self.assertTrue(config.is_known_window(config.WEEKLY_SCOPED_PREFIX + "fable"))
        self.assertFalse(config.is_known_window("nimbus_quill"))
        self.assertFalse(config.is_known_window("iguana_necktie"))

    def test_slug_roundtrip_single_word(self):
        slug = config.weekly_scoped_slug("Fable")
        self.assertEqual(slug, "fable")
        self.assertEqual(config.window_display_name(config.WEEKLY_SCOPED_PREFIX + slug), "Fable")

    def test_slug_roundtrip_punctuated_name(self):
        slug = config.weekly_scoped_slug("Claude Opus 4.5")
        self.assertEqual(slug, "claude_opus_4.5")
        self.assertEqual(
            config.window_display_name(config.WEEKLY_SCOPED_PREFIX + slug),
            "Claude Opus 4.5",
        )

    def test_label(self):
        self.assertEqual(config.label("five_hour"), "5-hour")
        self.assertEqual(config.label("seven_day"), "weekly")
        self.assertEqual(config.label(config.WEEKLY_SCOPED_PREFIX + "fable"), "week · Fable")
        self.assertEqual(config.label("nimbus_quill"), "nimbus_quill")

    def test_short_label(self):
        self.assertEqual(config.short_label("five_hour"), "5h")
        self.assertEqual(config.short_label("seven_day_sonnet"), "son")
        self.assertEqual(config.short_label(config.WEEKLY_SCOPED_PREFIX + "fable"), "Fab")
        # "Claude " prefix is dropped so distinct model families don't all
        # collapse to the same "Cla" token
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus"),
            "Opu",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.1"),
            "Op41",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.5"),
            "Op45",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_sonnet_4.5"),
            "So45",
        )
        # version-first naming must not collapse either ("Claude 3.5 Sonnet"
        # and "Claude 3.5 Haiku" would both short to "3.5")
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_3.5_sonnet"),
            "So35",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_3.5_haiku"),
            "Ha35",
        )
        # a blank slug must not crash (defensive; sources rejects blank names)
        self.assertEqual(config.short_label(config.WEEKLY_SCOPED_PREFIX), "wee")


if __name__ == "__main__":
    unittest.main()


class TestAccountKeys(unittest.TestCase):
    def test_split_and_with_account_roundtrip(self):
        cases = [
            ("five_hour", "work", "claude@work/five_hour"),
            ("weekly_scoped:claude_opus_4.5", "work", "claude@work/weekly_scoped:claude_opus_4.5"),
            ("ollama/weekly", "home", "ollama@home/weekly"),
            ("opencode-go/rolling", "opencode.work", "opencode-go@opencode.work/rolling"),
        ]
        for base, tag, key in cases:
            self.assertEqual(config.with_account(base, tag), key)
            self.assertEqual(config.split_account(key), (base, tag))

    def test_default_keys_unchanged(self):
        self.assertEqual(config.with_account("five_hour", "default"), "five_hour")
        self.assertEqual(config.split_account("ollama/weekly"), ("ollama/weekly", "default"))
        self.assertEqual(config.split_account("five_hour"), ("five_hour", "default"))

    def test_provider_of_ignores_account(self):
        self.assertEqual(config.provider_of("ollama@home/weekly"), "ollama")
        self.assertEqual(config.provider_of("claude@work/five_hour"), "claude")

    def test_label_appends_account(self):
        self.assertEqual(config.label("claude@work/five_hour"), config.label("five_hour") + " · work")
        self.assertTrue(config.is_known_window("claude@work/five_hour"))
        self.assertFalse(config.is_known_window("claude@work/bogus"))

    def test_period(self):
        self.assertEqual(config.period("claude@work/five_hour"), ("5h", 0))
        self.assertEqual(config.period("weekly_scoped:fable"), ("wk", 2))
        self.assertEqual(config.period("antigravity@joanna/gemini-weekly"), ("wk", 2))
        self.assertEqual(config.period("opencode-go@opencode.solo/rolling"), ("5h", 0))
        self.assertEqual(config.period("opencode-go/monthly"), ("mo", 3))

    def test_weekly_of_pairs_five_hour_windows(self):
        self.assertEqual(config.weekly_of("five_hour"), ["seven_day"])
        self.assertEqual(config.weekly_of("claude@work/five_hour"), ["claude@work/seven_day"])
        self.assertEqual(config.weekly_of("opencode-go/rolling"), ["opencode-go/weekly", "opencode-go/seven_day"])
        self.assertEqual(config.weekly_of("antigravity/gemini-5h"),
                         ["antigravity/gemini-weekly", "antigravity/gemini-seven_day"])
        self.assertEqual(config.weekly_of("seven_day"), [])
        self.assertEqual(config.weekly_of("ollama/session"), [])

    def test_is_weekly(self):
        for w in ("seven_day", "claude@work/seven_day", config.WEEKLY_SCOPED_PREFIX + "fable",
                  "ollama/weekly", "antigravity@joanna/gemini-weekly"):
            self.assertTrue(config.is_weekly(w), w)
        for w in ("five_hour", "antigravity/gemini-5h", "ollama/allowance"):
            self.assertFalse(config.is_weekly(w), w)

    def test_period_of(self):
        self.assertEqual(config.period_of("seven_day"), "week")
        self.assertEqual(config.period_of("ollama@home/weekly"), "week")
        self.assertEqual(config.period_of("ollama/allowance"), "month")
        self.assertEqual(config.period_of("opencode-go/monthly"), "month")
        self.assertIsNone(config.period_of("five_hour"))
        self.assertIsNone(config.period_of("openrouter/free_daily"))

    def test_account_tag(self):
        self.assertEqual(config.account_tag("claude", "work"), "work")
        self.assertEqual(config.account_tag("opencode", "work"), "opencode.work")
        self.assertEqual(config.account_tag("ollama", "default"), "default")
