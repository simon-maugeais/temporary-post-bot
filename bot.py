from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

import discord

from config import Settings, load_settings
from database import Database
from services.expiration import ExpirationWorker
from services.posts import PostService
from views import LifetimeView

logger = logging.getLogger(__name__)


class TemporaryPostBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = bool(settings.text_channel_ids)
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.database = Database(settings.database_path)
        self.posts = PostService(self, self.database, settings, datetime.now(UTC))
        self.worker = ExpirationWorker(self.posts)

    async def setup_hook(self) -> None:
        await self.database.open()
        self.add_view(LifetimeView(self.posts, self.posts.settings.duration_options))
        self.worker.start()
        logger.info("Persistent buttons registered; expiration worker started")

    async def on_ready(self) -> None:
        logger.info("Bot ready as %s (id=%s)", self.user, self.user.id if self.user else None)
        self.worker.request_recovery()

    async def on_resumed(self) -> None:
        logger.info("Discord connection resumed")
        self.worker.request_recovery()

    async def on_disconnect(self) -> None:
        logger.warning("Disconnected from Discord; saved schedules remain in SQLite")

    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not await self.posts.safely_detect_thread(thread):
            self.worker.request_recovery()

    async def on_message(self, message: discord.Message) -> None:
        if not await self.posts.safely_detect_message(message):
            self.worker.request_recovery()

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._message_deleted(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            await self._message_deleted(message_id)

    async def _message_deleted(self, message_id: int) -> None:
        try:
            await self.posts.message_deleted(message_id)
        except Exception as error:
            self.posts.log_failure("manual message deletion", message_id, error)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        try:
            await self.posts.thread_deleted(payload.thread_id)
        except Exception as error:
            self.posts.log_failure("manual thread deletion", payload.thread_id, error)

    async def close(self) -> None:
        try:
            await self.worker.stop()
            await super().close()
        finally:
            await self.database.close()


async def run_bot(settings: Settings) -> None:
    async with TemporaryPostBot(settings) as client:
        await client.start(settings.discord_token, reconnect=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, None)
        if not isinstance(level, int):
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        logging.getLogger().setLevel(level)
        logging.getLogger("discord.http").setLevel(logging.INFO)
        logger.info("Starting temporary post bot (Forums and text channels)")
        asyncio.run(run_bot(settings))
    except ValueError as error:
        logger.error("Configuration error: %s", error)
        raise SystemExit(1) from error
    except discord.LoginFailure:
        logger.error("Discord rejected the token. Check DISCORD_TOKEN in .env.")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception:
        logger.exception("Bot startup or shutdown failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
