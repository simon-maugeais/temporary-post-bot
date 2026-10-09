from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from weakref import WeakValueDictionary

import aiosqlite
import discord

from config import Settings
from database import Database, Post
from views import CUSTOM_ID_PREFIX, LifetimeView

logger = logging.getLogger(__name__)


def utc_timestamp() -> int:
    return int(datetime.now(UTC).timestamp())


def expiration_message(post: Post) -> str:
    if post.expires_at is None:
        raise ValueError("A scheduled post must have an expiration timestamp.")
    message = (
        f"🕒 This post will automatically be deleted on <t:{post.expires_at}:F> "
        f"(<t:{post.expires_at}:R>)."
    )
    if post.default_applied:
        hours = (post.default_expires_at - post.created_at) / 3600
        message += (
            f"\nNo duration was selected before <t:{post.selection_deadline}:F>, "
            f"so the default lifetime of {hours:g} hours from the original creation time was applied."
        )
    return message


class PostService:
    def __init__(
        self, client: discord.Client, database: Database, settings: Settings,
        started_at: datetime,
    ) -> None:
        self.client = client
        self.database = database
        self.settings = settings
        self.started_at = started_at
        self._locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._views: dict[int, LifetimeView] = {}

    def lock_for(self, thread_id: int) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    def is_configured(self, post: Post) -> bool:
        return post.forum_channel_id in self.settings.forum_channel_ids

    def _view_for(self, thread_id: int) -> LifetimeView:
        if thread_id not in self._views:
            self._views[thread_id] = LifetimeView(self, self.settings.duration_options)
        return self._views[thread_id]

    def _stop_view(self, thread_id: int) -> None:
        view = self._views.pop(thread_id, None)
        if view is not None:
            view.stop()

    @staticmethod
    def log_failure(operation: str, thread_id: int, error: Exception) -> None:
        if isinstance(error, discord.Forbidden):
            logger.error("Discord permission error during %s for thread %s: %s", operation, thread_id, error)
        elif isinstance(error, discord.HTTPException):
            logger.warning("Discord API error during %s for thread %s: %s", operation, thread_id, error)
        elif isinstance(error, aiosqlite.Error):
            logger.error("Database error during %s for thread %s: %s", operation, thread_id, error)
        else:
            logger.error(
                "Failure during %s for thread %s", operation, thread_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def detect_thread(self, thread: discord.Thread) -> None:
        if thread.parent_id not in self.settings.forum_channel_ids:
            return
        parent = thread.parent or await self.client.fetch_channel(thread.parent_id)
        if not isinstance(parent, discord.ForumChannel) or parent.type != discord.ChannelType.forum:
            return
        async with self.lock_for(thread.id):
            post = await self.database.get_post(thread.id)
            if post is None:
                created_at = thread.created_at or discord.utils.snowflake_time(thread.id)
                if created_at < self.started_at:
                    return
                await self.database.record_thread(
                    thread_id=thread.id,
                    guild_id=thread.guild.id,
                    forum_channel_id=thread.parent_id,
                    author_id=thread.owner_id,
                    created_at=int(created_at.timestamp()),
                    selection_timeout=self.settings.selection_timeout_seconds,
                    default_lifetime=self.settings.default_lifetime_seconds,
                )
                logger.info(
                    "New Forum post detected: thread=%s guild=%s forum=%s author=%s created=%s",
                    thread.id, thread.guild.id, thread.parent_id, thread.owner_id, created_at.isoformat(),
                )
                post = await self.database.get_post(thread.id)
            if post is not None and (post.status == "pending_author" or post.needs_sync):
                await self._sync_locked(post, thread)

    async def safely_detect_thread(self, thread: discord.Thread) -> bool:
        try:
            async with asyncio.timeout(30):
                await self.detect_thread(thread)
            return True
        except Exception as error:
            self.log_failure("post detection", thread.id, error)
            return False

    async def fetch_thread(self, post: Post) -> discord.Thread | None:
        try:
            channel = await self.client.fetch_channel(post.thread_id)
        except discord.NotFound as error:
            if error.code != 10003:
                raise
            await self.mark_missing(post.thread_id)
            return None
        if (
            not isinstance(channel, discord.Thread)
            or channel.guild.id != post.guild_id
            or channel.parent_id != post.forum_channel_id
        ):
            raise RuntimeError("Fetched channel does not match the stored Forum thread.")
        return channel

    async def _resolve_author(self, post: Post, thread: discord.Thread) -> None:
        author_id = post.author_id or thread.owner_id
        author = None
        if author_id is not None:
            author = self.client.get_user(author_id)
            if author is None:
                try:
                    author = await self.client.fetch_user(author_id)
                except discord.NotFound:
                    author = None
        if author is None:
            starter = thread.starter_message or await thread.fetch_message(thread.id)
            author = starter.author
        await self.database.resolve_author(thread.id, author.id, is_bot=author.bot)
        if author.bot:
            logger.info("Ignoring bot-created Forum post %s", thread.id)

    async def apply_default(self, thread_id: int, now: int) -> None:
        async with self.lock_for(thread_id):
            post = await self.database.get_post(thread_id)
            if post is not None and self.is_configured(post):
                await self._apply_default_locked(post, now)

    async def _apply_default_locked(self, post: Post, now: int) -> None:
        if await self.database.apply_default(post.thread_id, now):
            logger.info("Default lifetime applied to thread %s", post.thread_id)
            logger.info("Post %s scheduled for deletion at %s", post.thread_id, post.default_expires_at)

    async def sync_post(self, thread_id: int) -> None:
        async with self.lock_for(thread_id):
            post = await self.database.get_post(thread_id)
            if post is None or not self.is_configured(post):
                return
            if post.status in ("deleted", "ignored"):
                return
            thread = await self.fetch_thread(post)
            if thread is not None:
                await self._sync_locked(post, thread)

    async def _sync_locked(self, post: Post, thread: discord.Thread) -> None:
        if post.status == "pending_author":
            await self._resolve_author(post, thread)
        await self._apply_default_locked(post, utc_timestamp())
        current = await self.database.get_post(thread.id)
        if current is None or current.status not in ("awaiting_selection", "scheduled"):
            return
        if current.status == "scheduled" and current.expires_at is not None and current.expires_at <= utc_timestamp():
            return
        if not current.needs_sync:
            return
        if thread.archived:
            thread = await thread.edit(archived=False, reason="Update temporary post lifetime notice")
        content = (
            f"<@{current.author_id}> How long should this post remain available?"
            f"\nChoose before <t:{current.selection_deadline}:F> (<t:{current.selection_deadline}:R>)."
            " Lifetimes are measured from the post's original creation time."
            if current.status == "awaiting_selection" else expiration_message(current)
        )
        view = self._view_for(thread.id) if current.status == "awaiting_selection" else None
        message_id = current.prompt_message_id
        if message_id is None:
            message_id = await self._recover_prompt(thread)
            if message_id is not None:
                await self.database.set_prompt(thread.id, message_id)
        if message_id is not None:
            try:
                await thread.get_partial_message(message_id).edit(
                    content=content, view=view, allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound as error:
                if error.code != 10008:
                    raise
                message_id = None
        if message_id is None:
            message = await thread.send(
                content, view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=current.author_id)] if current.author_id is not None else False,
                    roles=False, everyone=False, replied_user=False,
                ),
            )
            await self.database.set_prompt(thread.id, message.id)
        await self.database.mark_synced(thread.id)
        if current.status == "scheduled":
            self._stop_view(thread.id)

    async def _recover_prompt(self, thread: discord.Thread) -> int | None:
        if self.client.user is None:
            raise RuntimeError("Discord client is not logged in.")
        async for message in thread.history(limit=None, oldest_first=True):
            if message.author.id != self.client.user.id:
                continue
            has_lifetime_buttons = any(
                getattr(component, "custom_id", "").startswith(CUSTOM_ID_PREFIX)
                for row in message.components
                for component in getattr(row, "children", [])
                if getattr(component, "custom_id", None) is not None
            )
            if has_lifetime_buttons or message.content.startswith("🕒 This post will automatically be deleted"):
                return message.id
        return None

    async def select_lifetime(self, interaction: discord.Interaction, seconds: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.channel_id is None or interaction.guild_id is None or interaction.message is None:
            await interaction.followup.send("This button must be used inside its Forum post.", ephemeral=True)
            return
        async with self.lock_for(interaction.channel_id):
            post = await self.database.get_post(interaction.channel_id)
            if post is None or not self.is_configured(post) or post.guild_id != interaction.guild_id:
                await interaction.followup.send("This post is not currently managed by the bot.", ephemeral=True)
                return
            if interaction.user.id != post.author_id:
                await interaction.followup.send("Only the post author can select its lifetime.", ephemeral=True)
                return
            if post.prompt_message_id != interaction.message.id:
                await interaction.followup.send("Please use the current lifetime-selection message.", ephemeral=True)
                return
            if seconds not in {option.seconds for option in self.settings.duration_options}:
                await interaction.followup.send("That lifetime option is no longer available.", ephemeral=True)
                return
            now = utc_timestamp()
            await self._apply_default_locked(post, now)
            selected = await self.database.select_lifetime(post.thread_id, post.author_id, seconds, now)
            current = await self.database.get_post(post.thread_id)
            if selected:
                logger.info("Lifetime selected: thread=%s author=%s seconds=%s", post.thread_id, post.author_id, seconds)
                logger.info("Post %s scheduled for deletion at %s", post.thread_id, current.expires_at if current else None)
            notice_failed = False
            if current is not None and current.status == "scheduled" and current.needs_sync:
                try:
                    thread = await self.fetch_thread(current)
                    if thread is not None:
                        await self._sync_locked(current, thread)
                except Exception as error:
                    notice_failed = True
                    self.log_failure("expiration confirmation (will retry)", post.thread_id, error)
            if current is None or current.status != "scheduled":
                content = "This post is no longer awaiting a lifetime selection."
            elif selected:
                content = expiration_message(current)
            elif current.default_applied:
                content = "The selection deadline has passed. " + expiration_message(current)
            else:
                content = "A lifetime has already been selected. " + expiration_message(current)
            if notice_failed:
                content += "\nThe schedule is saved; updating the public notice will be retried."
            await interaction.followup.send(content, ephemeral=True)

    async def delete_expired(self, thread_id: int, now: int) -> None:
        async with self.lock_for(thread_id):
            post = await self.database.get_post(thread_id)
            if (
                post is None or not self.is_configured(post) or post.status != "scheduled"
                or post.expires_at is None or post.expires_at > now
            ):
                return
            thread = await self.fetch_thread(post)
            if thread is None:
                return
            try:
                await thread.delete(reason="Temporary Forum post lifetime expired")
            except discord.NotFound as error:
                if error.code != 10003:
                    raise
                await self.mark_missing(thread_id)
                return
            await self.database.mark_deleted(thread_id, now)
            self._stop_view(thread_id)
            logger.info("Post successfully deleted: thread=%s", thread_id)

    async def mark_missing(self, thread_id: int) -> None:
        await self.database.mark_deleted(thread_id, utc_timestamp())
        self._stop_view(thread_id)
        logger.info("Post already missing: thread=%s", thread_id)

    async def thread_deleted(self, thread_id: int) -> None:
        async with self.lock_for(thread_id):
            post = await self.database.get_post(thread_id)
            if post is not None and post.status not in ("deleted", "ignored"):
                await self.mark_missing(thread_id)
