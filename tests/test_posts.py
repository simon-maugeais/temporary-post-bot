from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord

from bot import TemporaryPostBot
from config import Settings
from database import Database
from services.expiration import ExpirationWorker
from services.posts import PostService, expiration_message
from views import CUSTOM_ID_PREFIX, LifetimeView


async def history(*messages: object) -> AsyncIterator[object]:
    for message in messages:
        yield message


def discord_error(
    exception_type: type[discord.HTTPException], status: int, code: int,
) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test API error")
    return exception_type(response, {"code": code, "message": "Test API error"})


class PostServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            discord_token="test-token", forum_channel_ids=frozenset({30}),
            database_path=Path(self.directory.name) / "posts.sqlite3",
        )
        self.database = Database(self.settings.database_path)
        await self.database.open()
        self.created_at = datetime(2026, 10, 9, 12, tzinfo=UTC)
        self.now = int(self.created_at.timestamp()) + 1
        self.clock = patch("services.posts.utc_timestamp", return_value=self.now)
        self.clock.start()
        self.worker_clock = patch("services.expiration.utc_timestamp", return_value=self.now)
        self.worker_clock.start()
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id = 20
        self.guild.threads = []
        self.guild.me = None
        self.forum = MagicMock(spec=discord.ForumChannel)
        self.forum.id = 30
        self.forum.type = discord.ChannelType.forum
        self.forum.guild = self.guild
        self.forum.archived_threads.side_effect = lambda **kwargs: history()
        self.client = MagicMock(spec=discord.Client)
        self.client.user = SimpleNamespace(id=99)
        self.client.guilds = []
        self.client.get_user.return_value = None
        self.client.fetch_user.return_value = SimpleNamespace(id=40, bot=False)
        self.service = PostService(
            self.client, self.database, self.settings, self.created_at - timedelta(seconds=1),
        )
        self.thread = self.make_thread(self.created_at)
        self.client.fetch_channel.return_value = self.thread

    async def asyncTearDown(self) -> None:
        self.clock.stop()
        self.worker_clock.stop()
        for view in self.service._views.values():
            view.stop()
        await self.database.close()
        self.directory.cleanup()

    def make_thread(self, created_at: datetime, sequence: int = 0) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = discord.utils.time_snowflake(created_at) + sequence
        thread.guild = self.guild
        thread.parent = self.forum
        thread.parent_id = 30
        thread.owner_id = 40
        thread.created_at = created_at
        thread.archived = False
        thread.starter_message = None
        thread.history.side_effect = lambda **kwargs: history()
        thread.send.return_value = SimpleNamespace(id=50 + sequence)
        thread.get_partial_message.return_value.edit = AsyncMock()
        thread.edit.return_value = thread
        return thread

    def interaction(self, *, user_id: int = 40, message_id: int = 50) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel_id = self.thread.id
        interaction.guild_id = 20
        interaction.user = SimpleNamespace(id=user_id)
        interaction.message = SimpleNamespace(id=message_id)
        interaction.response = SimpleNamespace(defer=AsyncMock())
        interaction.followup = SimpleNamespace(send=AsyncMock())
        return interaction

    async def schedule(self, seconds: int = 3600) -> None:
        await self.service.detect_thread(self.thread)
        await self.database.select_lifetime(self.thread.id, 40, seconds, self.now)

    async def test_native_thread_detection_sends_persistent_buttons(self) -> None:
        await self.service.detect_thread(self.thread)
        post = await self.database.get_post(self.thread.id)
        self.assertEqual(post.status, "awaiting_selection")
        self.assertEqual(post.created_at, self.now - 1)
        self.assertEqual(post.prompt_message_id, 50)
        arguments = self.thread.send.call_args
        self.assertIn("How long should this post remain available?", arguments.args[0])
        self.assertTrue(arguments.kwargs["view"].is_persistent())
        self.assertEqual(len(arguments.kwargs["view"].children), 7)
        self.assertFalse(post.needs_sync)

    async def test_bot_authors_are_ignored_without_members_intent(self) -> None:
        self.client.fetch_user.return_value = SimpleNamespace(id=40, bot=True)
        await self.service.detect_thread(self.thread)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "ignored")
        self.thread.send.assert_not_awaited()

    async def test_author_falls_back_to_forum_starter_message(self) -> None:
        self.thread.owner_id = None
        self.thread.fetch_message.return_value = SimpleNamespace(author=SimpleNamespace(id=40, bot=False))
        await self.service.detect_thread(self.thread)
        self.thread.fetch_message.assert_awaited_once_with(self.thread.id)
        self.assertEqual((await self.database.get_post(self.thread.id)).author_id, 40)

    async def test_failed_author_lookup_remains_recoverable(self) -> None:
        self.client.fetch_user.side_effect = discord_error(discord.HTTPException, 500, 0)
        with self.assertLogs("services.posts", level="WARNING"):
            self.assertFalse(await self.service.safely_detect_thread(self.thread))
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "pending_author")
        self.client.fetch_user.side_effect = None
        await self.service.sync_post(self.thread.id)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "awaiting_selection")

    async def test_old_untracked_posts_are_ignored(self) -> None:
        self.thread.created_at = self.created_at - timedelta(days=1)
        await self.service.detect_thread(self.thread)
        self.assertIsNone(await self.database.get_post(self.thread.id))
        self.thread.send.assert_not_awaited()

    async def test_unconfigured_and_non_forum_threads_are_ignored(self) -> None:
        self.thread.parent_id = 31
        await self.service.detect_thread(self.thread)
        self.thread.parent_id = 30
        self.forum.type = discord.ChannelType.media
        await self.service.detect_thread(self.thread)
        self.assertIsNone(await self.database.get_post(self.thread.id))

    async def test_only_author_can_choose(self) -> None:
        await self.service.detect_thread(self.thread)
        interaction = self.interaction(user_id=41)
        await self.service.select_lifetime(interaction, 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "awaiting_selection")
        self.assertIn("Only the post author", interaction.followup.send.call_args.args[0])
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_stale_message_cannot_schedule(self) -> None:
        await self.service.detect_thread(self.thread)
        await self.service.select_lifetime(self.interaction(message_id=51), 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "awaiting_selection")

    async def test_double_click_does_not_overwrite_schedule(self) -> None:
        await self.service.detect_thread(self.thread)
        interactions = (self.interaction(), self.interaction())
        await asyncio.gather(
            self.service.select_lifetime(interactions[0], 3600),
            self.service.select_lifetime(interactions[1], 21600),
        )
        post = await self.database.get_post(self.thread.id)
        self.assertEqual(post.expires_at, self.now - 1 + 3600)
        edit = self.thread.get_partial_message.return_value.edit.call_args
        self.assertIsNone(edit.kwargs["view"])
        self.assertIn(f"<t:{post.expires_at}:F>", edit.kwargs["content"])
        self.assertIn("already been selected", interactions[1].followup.send.call_args.args[0])

    async def test_late_click_applies_default_even_before_worker(self) -> None:
        await self.service.detect_thread(self.thread)
        with patch("services.posts.utc_timestamp", return_value=self.now - 1 + 3600):
            await self.service.select_lifetime(self.interaction(), 3600)
        post = await self.database.get_post(self.thread.id)
        self.assertTrue(post.default_applied)
        self.assertEqual(post.expires_at, post.created_at + 86400)
        self.assertIn("default lifetime", expiration_message(post))
        self.assertIsNone(self.thread.get_partial_message.return_value.edit.call_args.kwargs["view"])

    async def test_failed_public_confirmation_keeps_saved_schedule_and_retries(self) -> None:
        await self.service.detect_thread(self.thread)
        edit = self.thread.get_partial_message.return_value.edit
        edit.side_effect = discord_error(discord.Forbidden, 403, 50013)
        interaction = self.interaction()
        with self.assertLogs("services.posts", level="ERROR"):
            await self.service.select_lifetime(interaction, 3600)
        post = await self.database.get_post(self.thread.id)
        self.assertEqual(post.status, "scheduled")
        self.assertTrue(post.needs_sync)
        self.assertIn("schedule is saved", interaction.followup.send.call_args.args[0])
        edit.side_effect = None
        await self.service.sync_post(self.thread.id)
        self.assertFalse((await self.database.get_post(self.thread.id)).needs_sync)

    async def test_persistent_buttons_work_with_restarted_service(self) -> None:
        await self.service.detect_thread(self.thread)
        await self.database.close()
        await self.database.open()
        restarted = PostService(
            self.client, self.database, self.settings, self.created_at + timedelta(days=1),
        )
        view = LifetimeView(restarted, self.settings.duration_options)
        await view.children[0].callback(self.interaction())
        self.assertEqual((await self.database.get_post(self.thread.id)).expires_at, self.now - 1 + 300)
        self.assertIsNone(self.thread.get_partial_message.return_value.edit.call_args.kwargs["view"])
        view.stop()

    async def test_recovers_prompt_sent_before_crash(self) -> None:
        orphan = SimpleNamespace(
            id=55, author=SimpleNamespace(id=99), content="prompt",
            components=[SimpleNamespace(children=[SimpleNamespace(custom_id=f"{CUSTOM_ID_PREFIX}3600")])],
        )
        self.thread.history.side_effect = lambda **kwargs: history(orphan)
        await self.service.detect_thread(self.thread)
        self.thread.send.assert_not_awaited()
        self.assertEqual((await self.database.get_post(self.thread.id)).prompt_message_id, 55)
        self.thread.get_partial_message.assert_called_once_with(55)

    async def test_missing_prompt_is_replaced_without_deleting_post(self) -> None:
        await self.service.detect_thread(self.thread)
        await self.database.execute("UPDATE posts SET needs_sync = 1 WHERE thread_id = ?", (self.thread.id,))
        self.thread.get_partial_message.return_value.edit.side_effect = discord_error(discord.NotFound, 404, 10008)
        self.thread.send.return_value = SimpleNamespace(id=56)
        await self.service.sync_post(self.thread.id)
        post = await self.database.get_post(self.thread.id)
        self.assertEqual(post.status, "awaiting_selection")
        self.assertEqual(post.prompt_message_id, 56)

    async def test_expiration_deletes_whole_archived_thread(self) -> None:
        await self.schedule()
        self.thread.archived = True
        await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.thread.delete.assert_awaited_once_with(reason="Temporary Forum post lifetime expired")
        self.thread.edit.assert_not_awaited()
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "deleted")

    async def test_permission_failure_keeps_deletion_pending(self) -> None:
        await self.schedule()
        self.thread.delete.side_effect = discord_error(discord.Forbidden, 403, 50013)
        with self.assertRaises(discord.Forbidden):
            await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "scheduled")
        self.thread.delete.side_effect = None
        await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "deleted")

    async def test_missing_thread_is_marked_deleted(self) -> None:
        await self.schedule()
        self.client.fetch_channel.side_effect = discord_error(discord.NotFound, 404, 10003)
        await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "deleted")
        self.thread.delete.assert_not_awaited()

    async def test_non_channel_not_found_does_not_forget_schedule(self) -> None:
        await self.schedule()
        self.client.fetch_channel.side_effect = discord_error(discord.NotFound, 404, 10008)
        with self.assertRaises(discord.NotFound):
            await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "scheduled")

    async def test_delete_success_with_database_failure_recovers_on_next_attempt(self) -> None:
        await self.schedule()
        with patch.object(self.database, "mark_deleted", side_effect=aiosqlite.OperationalError("test failure")):
            with self.assertRaises(aiosqlite.OperationalError):
                await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "scheduled")
        self.client.fetch_channel.side_effect = discord_error(discord.NotFound, 404, 10003)
        await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "deleted")

    async def test_removed_forum_pauses_saved_schedule(self) -> None:
        await self.schedule()
        self.service.settings = Settings(
            discord_token="test-token", forum_channel_ids=frozenset({31}),
            database_path=self.settings.database_path,
        )
        await self.service.delete_expired(self.thread.id, self.now + 3600)
        self.thread.delete.assert_not_awaited()
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "scheduled")

    async def test_worker_isolates_failed_deletion_and_retries(self) -> None:
        await self.schedule()
        other = self.make_thread(self.created_at, sequence=1)
        await self.service.detect_thread(other)
        await self.database.select_lifetime(other.id, 40, 3600, self.now)
        self.thread.delete.side_effect = discord_error(discord.HTTPException, 500, 0)
        self.client.fetch_channel.side_effect = lambda thread_id: self.thread if thread_id == self.thread.id else other
        worker = ExpirationWorker(self.service)
        worker._recovery_needed = False
        with patch("services.expiration.utc_timestamp", return_value=self.now + 3600):
            with patch("services.posts.utc_timestamp", return_value=self.now + 3600):
                with self.assertLogs("services.posts", level="WARNING"):
                    await worker.tick()
                self.assertEqual((await self.database.get_post(self.thread.id)).status, "scheduled")
                self.assertEqual((await self.database.get_post(other.id)).status, "deleted")
                self.thread.delete.side_effect = None
                await worker.tick()
        self.assertEqual((await self.database.get_post(self.thread.id)).status, "deleted")

    async def test_worker_applies_default_and_updates_notice(self) -> None:
        await self.service.detect_thread(self.thread)
        worker = ExpirationWorker(self.service)
        worker._recovery_needed = False
        with patch("services.expiration.utc_timestamp", return_value=self.now + 3600):
            with patch("services.posts.utc_timestamp", return_value=self.now + 3600):
                await worker.tick()
        post = await self.database.get_post(self.thread.id)
        self.assertTrue(post.default_applied)
        self.assertFalse(post.needs_sync)
        self.assertIn("default lifetime", self.thread.get_partial_message.return_value.edit.call_args.kwargs["content"])

    async def test_minimum_intents_and_persistent_startup_registration(self) -> None:
        client = TemporaryPostBot(self.settings)
        await client.setup_hook()
        try:
            self.assertEqual(client.intents.value, discord.Intents(guilds=True).value)
            self.assertEqual(len(client.persistent_views), 1)
            self.assertTrue(client.persistent_views[0].is_persistent())
        finally:
            await client.close()

    async def test_recovery_checks_active_and_recent_archived_threads(self) -> None:
        old = self.make_thread(self.created_at - timedelta(days=1))
        recent_archived = self.make_thread(self.created_at, sequence=1)
        recent_archived.archive_timestamp = self.created_at + timedelta(minutes=1)
        old_archived = self.make_thread(self.created_at - timedelta(days=2))
        old_archived.archive_timestamp = self.created_at - timedelta(days=1)
        self.guild.active_threads.return_value = [old, self.thread]
        self.forum.archived_threads.side_effect = lambda **kwargs: history(recent_archived, old_archived)
        self.client.fetch_channel.return_value = self.forum
        worker = ExpirationWorker(self.service)
        await worker.recover_new_threads()
        self.assertIsNone(await self.database.get_post(old.id))
        self.assertIsNone(await self.database.get_post(old_archived.id))
        self.assertIsNotNone(await self.database.get_post(self.thread.id))
        self.assertIsNotNone(await self.database.get_post(recent_archived.id))
