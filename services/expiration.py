from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord
from discord.ext import tasks

from services.posts import PostService, utc_timestamp

logger = logging.getLogger(__name__)


class ExpirationWorker:
    def __init__(self, service: PostService) -> None:
        self.service = service
        self._recovery_needed = True
        self.run.change_interval(seconds=service.settings.check_interval_seconds)

    def request_recovery(self) -> None:
        self._recovery_needed = True

    def start(self) -> None:
        self.run.start()

    async def stop(self) -> None:
        task = self.run.get_task()
        self.run.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    @tasks.loop(seconds=30)
    async def run(self) -> None:
        if not self.service.client.is_ready():
            return
        try:
            await self.tick()
        except Exception:
            logger.exception("Expiration worker cycle failed; persisted work will be retried")

    @run.before_loop
    async def before_run(self) -> None:
        await self.service.client.wait_until_ready()

    async def _attempt(
        self, operation: str, thread_id: int, action: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            async with asyncio.timeout(30):
                await action()
        except Exception as error:
            self.service.log_failure(operation, thread_id, error)

    async def tick(self) -> None:
        now = utc_timestamp()
        for post in await self.service.database.overdue_selections(now):
            await self._attempt(
                "default lifetime", post.thread_id,
                lambda post=post: self.service.apply_default(post.thread_id, now),
            )
        for post in await self.service.database.expired_posts(now):
            await self._attempt(
                "expiration deletion (will retry)", post.thread_id,
                lambda post=post: self.service.delete_expired(post.thread_id, now),
            )
        if self._recovery_needed:
            self._recovery_needed = False
            try:
                await self.recover_new_threads()
            except Exception:
                self._recovery_needed = True
                logger.exception("Thread recovery failed; retrying next worker cycle")
        for guild in self.service.client.guilds:
            for thread in guild.threads:
                await self.service.safely_detect_thread(thread)
        for post in await self.service.database.posts_to_sync():
            if post.status == "scheduled" and post.expires_at is not None and post.expires_at <= utc_timestamp():
                continue
            await self._attempt(
                "post message synchronization (will retry)", post.thread_id,
                lambda post=post: self.service.sync_post(post.thread_id),
            )

    async def recover_new_threads(self) -> None:
        forums: list[discord.ForumChannel] = []
        failed = False
        for channel_id in self.service.settings.forum_channel_ids:
            try:
                async with asyncio.timeout(30):
                    channel = await self.service.client.fetch_channel(channel_id)
            except Exception as error:
                self.service.log_failure("Forum recovery", channel_id, error)
                failed = True
                continue
            if not isinstance(channel, discord.ForumChannel) or channel.type != discord.ChannelType.forum:
                logger.error("Configured channel %s is not a Forum channel", channel_id)
                continue
            forums.append(channel)
            member = channel.guild.me
            if member is not None:
                permissions = channel.permissions_for(member)
                missing = [
                    name for name in ("view_channel", "read_message_history", "send_messages_in_threads", "manage_threads")
                    if not getattr(permissions, name)
                ]
                if missing:
                    logger.error("Forum %s is missing bot permissions: %s", channel_id, ", ".join(missing))
        guilds = {forum.guild.id: forum.guild for forum in forums}
        for guild in guilds.values():
            try:
                async with asyncio.timeout(30):
                    threads = await guild.active_threads()
                for thread in threads:
                    if not await self.service.safely_detect_thread(thread):
                        failed = True
            except Exception as error:
                self.service.log_failure("active thread recovery", guild.id, error)
                failed = True
        for forum in forums:
            try:
                async with asyncio.timeout(30):
                    async for thread in forum.archived_threads(limit=None):
                        if thread.archive_timestamp < self.service.started_at:
                            break
                        if not await self.service.safely_detect_thread(thread):
                            failed = True
            except Exception as error:
                self.service.log_failure("archived thread recovery", forum.id, error)
                failed = True
        if failed:
            self.request_recovery()
        else:
            logger.info("Thread recovery completed; pre-start posts without database records were ignored")
