from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import BASE_DIR, DurationOption, Settings, load_settings


class ConfigTests(unittest.TestCase):
    def test_environment_parsing_and_relative_paths(self) -> None:
        with patch.dict(os.environ, {
            "DISCORD_TOKEN": "test-token", "FORUM_CHANNEL_IDS": "10, 20,10",
            "DATABASE_PATH": "data/test.sqlite3", "SELECTION_TIMEOUT_HOURS": "0.5",
            "DEFAULT_POST_LIFETIME_HOURS": "48", "EXPIRATION_CHECK_INTERVAL_SECONDS": "60",
        }, clear=True):
            with patch("config.load_dotenv"):
                settings = load_settings()
        self.assertEqual(settings.forum_channel_ids, frozenset({10, 20}))
        self.assertEqual(settings.database_path, BASE_DIR / "data/test.sqlite3")
        self.assertEqual(settings.selection_timeout_seconds, 1800)
        self.assertEqual(settings.default_lifetime_seconds, 172800)
        self.assertEqual(settings.check_interval_seconds, 60)
        self.assertNotIn("test-token", repr(settings))

    def test_missing_token_and_bad_ids_fail_clearly(self) -> None:
        for token, ids in (("", "10"), ("test-token", "not-an-id"), ("test-token", "0")):
            with self.subTest(token_present=bool(token), ids=ids):
                with patch.dict(os.environ, {"DISCORD_TOKEN": token, "FORUM_CHANNEL_IDS": ids}, clear=True):
                    with patch("config.load_dotenv"):
                        with self.assertRaises(ValueError):
                            load_settings()

    def test_invalid_timing_and_volatile_database_are_rejected(self) -> None:
        for overrides in (
            {"SELECTION_TIMEOUT_HOURS": "0"},
            {"DEFAULT_POST_LIFETIME_HOURS": "nan"},
            {"EXPIRATION_CHECK_INTERVAL_SECONDS": "inf"},
            {"DATABASE_PATH": ":memory:"},
        ):
            with self.subTest(overrides=overrides):
                with patch.dict(os.environ, {
                    "DISCORD_TOKEN": "test-token", "FORUM_CHANNEL_IDS": "10", **overrides,
                }, clear=True):
                    with patch("config.load_dotenv"):
                        with self.assertRaises(ValueError):
                            load_settings()

    def test_invalid_duration_choices_are_rejected(self) -> None:
        for options in (
            (),
            (DurationOption("zero", 0),),
            (DurationOption("one", 3600), DurationOption("duplicate", 3600)),
            tuple(DurationOption(str(index), index + 1) for index in range(26)),
        ):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    Settings(
                        discord_token="test-token", forum_channel_ids=frozenset({10}),
                        database_path=Path("test.sqlite3"), duration_options=options,
                    )

    def test_text_only_configuration_is_supported(self) -> None:
        with patch.dict(os.environ, {
            "DISCORD_TOKEN": "test-token", "FORUM_CHANNEL_IDS": "",
            "TEXT_CHANNEL_IDS": "31, 32,31",
        }, clear=True):
            with patch("config.load_dotenv"):
                settings = load_settings()
        self.assertEqual(settings.forum_channel_ids, frozenset())
        self.assertEqual(settings.text_channel_ids, frozenset({31, 32}))

    def test_invalid_or_overlapping_text_channel_ids_are_rejected(self) -> None:
        for text_ids in ("invalid", "0", "10"):
            with self.subTest(text_ids=text_ids):
                with patch.dict(os.environ, {
                    "DISCORD_TOKEN": "test-token", "FORUM_CHANNEL_IDS": "10",
                    "TEXT_CHANNEL_IDS": text_ids,
                }, clear=True):
                    with patch("config.load_dotenv"):
                        with self.assertRaises(ValueError):
                            load_settings()
