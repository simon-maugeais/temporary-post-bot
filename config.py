from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SELECTION_TIMEOUT_HOURS = 1
DEFAULT_POST_LIFETIME_HOURS = 24
EXPIRATION_CHECK_INTERVAL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class DurationOption:
    label: str
    seconds: int


DURATION_OPTIONS = (
    DurationOption("30 secondes", 30),
    DurationOption("1 minutes", 1 * 60),
    DurationOption("5 minutes", 5 * 60),
    DurationOption("1 hour", 60 * 60),
    DurationOption("6 hours", 6 * 60 * 60),
    DurationOption("12 hours", 12 * 60 * 60),
    DurationOption("24 hours", 24 * 60 * 60),
    DurationOption("3 days", 3 * 24 * 60 * 60),
    DurationOption("7 days", 7 * 24 * 60 * 60),
)


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str = field(repr=False)
    forum_channel_ids: frozenset[int]
    database_path: Path
    text_channel_ids: frozenset[int] = frozenset()
    selection_timeout_seconds: int = SELECTION_TIMEOUT_HOURS * 3600
    default_lifetime_seconds: int = DEFAULT_POST_LIFETIME_HOURS * 3600
    check_interval_seconds: float = EXPIRATION_CHECK_INTERVAL_SECONDS
    duration_options: tuple[DurationOption, ...] = DURATION_OPTIONS

    def __post_init__(self) -> None:
        if not self.discord_token.strip():
            raise ValueError("DISCORD_TOKEN must be set.")
        if not self.forum_channel_ids and not self.text_channel_ids:
            raise ValueError("Configure at least one ID in FORUM_CHANNEL_IDS or TEXT_CHANNEL_IDS.")
        for name, channel_ids in (
            ("FORUM_CHANNEL_IDS", self.forum_channel_ids),
            ("TEXT_CHANNEL_IDS", self.text_channel_ids),
        ):
            if any(channel_id <= 0 or channel_id >= 2**63 for channel_id in channel_ids):
                raise ValueError(f"{name} must contain valid Discord IDs.")
        if self.forum_channel_ids & self.text_channel_ids:
            raise ValueError("A channel cannot be configured as both a Forum and a text channel.")
        if self.selection_timeout_seconds <= 0 or self.default_lifetime_seconds <= 0:
            raise ValueError("Selection timeout and default lifetime must be positive.")
        if not math.isfinite(self.check_interval_seconds) or self.check_interval_seconds <= 0:
            raise ValueError("Expiration check interval must be finite and positive.")
        if not 1 <= len(self.duration_options) <= 25:
            raise ValueError("Configure between 1 and 25 lifetime buttons.")
        if len({option.seconds for option in self.duration_options}) != len(self.duration_options):
            raise ValueError("Each lifetime button must have a unique duration.")
        for option in self.duration_options:
            if (
                not isinstance(option.seconds, int)
                or option.seconds <= 0
                or option.seconds >= 2**63
                or not 1 <= len(option.label) <= 80
            ):
                raise ValueError("Lifetime buttons need positive integer seconds and 1–80 character labels.")


def _positive_number(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number.") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")
    return value


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    channel_ids: dict[str, frozenset[int]] = {}
    for name in ("FORUM_CHANNEL_IDS", "TEXT_CHANNEL_IDS"):
        try:
            channel_ids[name] = frozenset(
                int(value.strip()) for value in os.environ.get(name, "").split(",")
                if value.strip()
            )
        except ValueError as error:
            raise ValueError(f"{name} must be comma-separated integer IDs.") from error
    raw_path = os.environ.get("DATABASE_PATH", "data/posts.sqlite3").strip()
    if not raw_path or raw_path == ":memory:":
        raise ValueError("DATABASE_PATH must name a persistent SQLite file.")
    database_path = Path(raw_path).expanduser()
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path
    return Settings(
        discord_token=token,
        forum_channel_ids=channel_ids["FORUM_CHANNEL_IDS"],
        text_channel_ids=channel_ids["TEXT_CHANNEL_IDS"],
        database_path=database_path,
        selection_timeout_seconds=int(_positive_number("SELECTION_TIMEOUT_HOURS", SELECTION_TIMEOUT_HOURS) * 3600),
        default_lifetime_seconds=int(_positive_number("DEFAULT_POST_LIFETIME_HOURS", DEFAULT_POST_LIFETIME_HOURS) * 3600),
        check_interval_seconds=_positive_number("EXPIRATION_CHECK_INTERVAL_SECONDS", EXPIRATION_CHECK_INTERVAL_SECONDS),
    )
