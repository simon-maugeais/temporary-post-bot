from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot import TemporaryPostBot
from config import Settings
from database import Database
from services.expiration import ExpirationWorker
from services.posts import PostService
from tests.test_posts import discord_error, history
from views import CUSTOM_ID_PREFIX, LifetimeView


class TextMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            discord_token="test-token", forum_channel_ids=frozenset({30}),
            text_channel_ids=frozenset({31}),
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
        self.guild.me = None
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 31
        self.channel.type = discord.ChannelType.text
        self.channel.guild = self.guild
        self.channel.history.side_effect = lambda **kwargs: history()
        self.sources: dict[int, MagicMock] = {}
        self.partials: dict[int, MagicMock] = {}
        self.channel.get_partial_message.side_effect = self.partial
        self.channel.fetch_message.side_effect = lambda message_id: self.sources[message_id]
        self.next_prompt_id = discord.utils.time_snowflake(self.created_at) + 100
        self.channel.send.side_effect = self.send_notice
        self.client = MagicMock(spec=discord.Client)
        self.client.user = SimpleNamespace(id=99)
        self.client.guilds = []
        self.client.fetch_channel.return_value = self.channel
        self.service = PostService(
            self.client, self.database, self.settings, self.created_at - timedelta(seconds=1),
        )
        self.message = self.make_message()

    async def asyncTearDown(self) -> None:
        self.clock.stop()
        self.worker_clock.stop()
        for view in self.service._views.values():
            view.stop()
        await self.database.close()
        self.directory.cleanup()

    def make_message(self, sequence: int = 0, author_id: int = 40) -> MagicMock:
        message = MagicMock(spec=discord.Message)
        message.id = discord.utils.time_snowflake(self.created_at) + sequence
        message.guild = self.guild
        message.channel = self.channel
        message.author = SimpleNamespace(id=author_id, bot=False)
        message.created_at = self.created_at
        message.webhook_id = None
        message.type = discord.MessageType.default
        message.content = ""
        message.attachments = []
        self.sources[message.id] = message
        return message

    def partial(self, message_id: int) -> MagicMock:
        if message_id not in self.partials:
            self.partials[message_id] = MagicMock(spec=discord.PartialMessage)
        return self.partials[message_id]

    async def send_notice(self, *args: object, **kwargs: object) -> SimpleNamespace:
        self.next_prompt_id += 1
        return SimpleNamespace(id=self.next_prompt_id)

    def interaction(self, prompt_id: int, author_id: int = 40) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel_id = self.channel.id
        interaction.guild_id = self.guild.id
        interaction.user = SimpleNamespace(id=author_id)
        interaction.message = SimpleNamespace(id=prompt_id)
        interaction.response = SimpleNamespace(defer=AsyncMock())
        interaction.followup = SimpleNamespace(send=AsyncMock())
        return interaction

    async def schedule(self) -> int:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        await self.service.select_lifetime(self.interaction(post.prompt_message_id), 300)
        return post.prompt_message_id

    async def test_detects_message_without_reading_content_and_replies_with_buttons(self) -> None:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        self.assertEqual(post.kind, "text_message")
        self.assertEqual(post.channel_id, 31)
        self.assertEqual(post.status, "awaiting_selection")
        call = self.channel.send.call_args
        self.assertIn("How long should this message remain available?", call.args[0])
        self.assertEqual(call.kwargs["reference"].message_id, self.message.id)
        self.assertTrue(call.kwargs["view"].is_persistent())
        self.message.edit.assert_not_awaited()

    async def test_bot_webhook_system_and_untracked_old_messages_are_ignored(self) -> None:
        messages = [self.make_message(sequence=index) for index in range(1, 5)]
        messages[0].author = SimpleNamespace(id=99, bot=True)
        messages[1].webhook_id = 100
        messages[2].type = discord.MessageType.pins_add
        messages[3].created_at = self.created_at - timedelta(days=1)
        for message in messages:
            await self.service.detect_message(message)
            self.assertIsNone(await self.database.get_post(message.id))
        self.channel.send.assert_not_awaited()

    async def test_messages_in_forum_threads_dms_and_other_channels_are_ignored(self) -> None:
        self.message.channel = MagicMock(spec=discord.Thread)
        await self.service.detect_message(self.message)
        self.message.channel = self.channel
        self.message.guild = None
        await self.service.detect_message(self.message)
        self.message.guild = self.guild
        self.channel.id = 32
        await self.service.detect_message(self.message)
        self.assertIsNone(await self.database.get_post(self.message.id))

    async def test_messages_in_one_channel_have_independent_lifetimes(self) -> None:
        other = self.make_message(sequence=1, author_id=41)
        await self.service.detect_message(self.message)
        await self.service.detect_message(other)
        first = await self.database.get_post(self.message.id)
        second = await self.database.get_post(other.id)
        self.assertNotEqual(first.prompt_message_id, second.prompt_message_id)
        wrong_author = self.interaction(first.prompt_message_id, author_id=41)
        await self.service.select_lifetime(wrong_author, 300)
        self.assertIn("Only the message author", wrong_author.followup.send.call_args.args[0])
        await self.service.select_lifetime(self.interaction(first.prompt_message_id), 300)
        await self.service.select_lifetime(self.interaction(second.prompt_message_id, author_id=41), 3600)
        first = await self.database.get_post(self.message.id)
        second = await self.database.get_post(other.id)
        self.assertEqual(first.expires_at, first.created_at + 300)
        self.assertEqual(second.expires_at, second.created_at + 3600)

    async def test_double_click_cannot_change_message_expiration(self) -> None:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        await asyncio.gather(
            self.service.select_lifetime(self.interaction(post.prompt_message_id), 300),
            self.service.select_lifetime(self.interaction(post.prompt_message_id), 3600),
        )
        self.assertEqual((await self.database.get_post(self.message.id)).expires_at, self.now - 1 + 300)

    async def test_wrong_interaction_channel_cannot_schedule(self) -> None:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        interaction = self.interaction(post.prompt_message_id)
        interaction.channel_id = 32
        await self.service.select_lifetime(interaction, 300)
        self.assertEqual((await self.database.get_post(self.message.id)).status, "awaiting_selection")

    async def test_text_buttons_and_schedule_survive_restart(self) -> None:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        await self.database.close()
        await self.database.open()
        restarted = PostService(
            self.client, self.database, self.settings, self.created_at + timedelta(days=1),
        )
        view = LifetimeView(restarted, self.settings.duration_options)
        await view.children[0].callback(self.interaction(post.prompt_message_id))
        self.assertEqual((await self.database.get_post(self.message.id)).expires_at, self.now - 1 + 300)
        self.assertIsNone(self.partial(post.prompt_message_id).edit.call_args.kwargs["view"])
        await self.database.close()
        await self.database.open()
        self.assertEqual((await self.database.get_post(self.message.id)).status, "scheduled")
        view.stop()

    async def test_deletion_removes_only_target_and_its_notice(self) -> None:
        prompt_id = await self.schedule()
        unrelated = self.make_message(sequence=1)
        await self.service.delete_expired(self.message.id, self.now + 300)
        self.partial(self.message.id).delete.assert_awaited_once()
        self.partial(prompt_id).delete.assert_awaited_once()
        self.partial(unrelated.id).delete.assert_not_awaited()
        self.channel.delete.assert_not_awaited()
        self.assertEqual((await self.database.get_post(self.message.id)).status, "deleted")

    async def test_target_permission_failure_preserves_schedule_and_notice(self) -> None:
        prompt_id = await self.schedule()
        self.partial(self.message.id).delete.side_effect = discord_error(discord.Forbidden, 403, 50013)
        with self.assertRaises(discord.Forbidden):
            await self.service.delete_expired(self.message.id, self.now + 300)
        post = await self.database.get_post(self.message.id)
        self.assertEqual(post.status, "scheduled")
        self.assertFalse(post.target_deleted)
        self.partial(prompt_id).delete.assert_not_awaited()

    async def test_notice_cleanup_failure_is_persisted_and_retried_after_restart(self) -> None:
        prompt_id = await self.schedule()
        self.partial(prompt_id).delete.side_effect = discord_error(discord.HTTPException, 500, 0)
        with self.assertRaises(discord.HTTPException):
            await self.service.delete_expired(self.message.id, self.now + 300)
        await self.database.close()
        await self.database.open()
        post = await self.database.get_post(self.message.id)
        self.assertTrue(post.target_deleted)
        self.assertEqual(post.status, "scheduled")
        self.partial(prompt_id).delete.side_effect = None
        await self.service.delete_expired(self.message.id, self.now + 301)
        self.partial(self.message.id).delete.assert_awaited_once()
        self.assertEqual((await self.database.get_post(self.message.id)).status, "deleted")

    async def test_already_missing_message_still_cleans_up_notice(self) -> None:
        prompt_id = await self.schedule()
        self.partial(self.message.id).delete.side_effect = discord_error(discord.NotFound, 404, 10008)
        await self.service.delete_expired(self.message.id, self.now + 300)
        self.partial(prompt_id).delete.assert_awaited_once()
        self.assertEqual((await self.database.get_post(self.message.id)).status, "deleted")

    async def test_manual_deletion_schedules_immediate_notice_cleanup(self) -> None:
        await self.service.detect_message(self.message)
        post = await self.database.get_post(self.message.id)
        await self.service.message_deleted(self.message.id)
        current = await self.database.get_post(self.message.id)
        self.assertTrue(current.target_deleted)
        self.assertEqual(current.expires_at, self.now)
        await self.service.delete_expired(self.message.id, self.now)
        self.partial(self.message.id).delete.assert_not_awaited()
        self.partial(post.prompt_message_id).delete.assert_awaited_once()

    async def test_missing_source_during_sync_does_not_recreate_notice(self) -> None:
        await self.service.detect_message(self.message)
        await self.database.request_sync(self.message.id)
        self.channel.fetch_message.side_effect = discord_error(discord.NotFound, 404, 10008)
        await self.service.sync_post(self.message.id)
        self.assertTrue((await self.database.get_post(self.message.id)).target_deleted)
        self.channel.send.assert_awaited_once()

    async def test_manually_deleted_notice_is_replaced_without_losing_schedule(self) -> None:
        prompt_id = await self.schedule()
        await self.service.message_deleted(prompt_id)
        self.partial(prompt_id).edit.side_effect = discord_error(discord.NotFound, 404, 10008)
        await self.service.sync_post(self.message.id)
        post = await self.database.get_post(self.message.id)
        self.assertNotEqual(post.prompt_message_id, prompt_id)
        self.assertEqual(post.expires_at, self.now - 1 + 300)
        self.assertFalse(post.needs_sync)

    async def test_orphan_prompt_recovery_matches_its_reply_reference(self) -> None:
        wrong = SimpleNamespace(
            id=200, author=SimpleNamespace(id=99), content="prompt",
            reference=SimpleNamespace(message_id=self.message.id + 1),
            components=[SimpleNamespace(children=[SimpleNamespace(custom_id=f"{CUSTOM_ID_PREFIX}300")])],
        )
        correct = SimpleNamespace(
            id=201, author=SimpleNamespace(id=99), content="prompt",
            reference=SimpleNamespace(message_id=self.message.id), components=wrong.components,
        )
        self.channel.history.side_effect = lambda **kwargs: history(wrong, correct)
        await self.service.detect_message(self.message)
        self.channel.send.assert_not_awaited()
        self.assertEqual((await self.database.get_post(self.message.id)).prompt_message_id, 201)

    async def test_worker_applies_default_for_text_message(self) -> None:
        await self.service.detect_message(self.message)
        worker = ExpirationWorker(self.service)
        worker._recovery_needed = False
        with patch("services.expiration.utc_timestamp", return_value=self.now + 3600):
            with patch("services.posts.utc_timestamp", return_value=self.now + 3600):
                await worker.tick()
        post = await self.database.get_post(self.message.id)
        self.assertTrue(post.default_applied)
        self.assertEqual(post.expires_at, post.created_at + 86400)
        self.assertIn("default lifetime", self.partial(post.prompt_message_id).edit.call_args.kwargs["content"])

    async def test_recovery_resumes_from_last_successful_message(self) -> None:
        worker = ExpirationWorker(self.service)
        self.channel.history.side_effect = lambda **kwargs: history(self.message)
        with patch.object(self.service, "safely_detect_message", return_value=True) as detect:
            await worker.recover_new_messages()
            detect.assert_awaited_once_with(self.message)
        self.assertEqual(self.channel.history.call_args.kwargs["after"], self.service.started_at)
        self.channel.history.side_effect = lambda **kwargs: history()
        await worker.recover_new_messages()
        self.assertEqual(self.channel.history.call_args.kwargs["after"].id, self.message.id)

    async def test_recovery_does_not_advance_past_a_failed_message(self) -> None:
        worker = ExpirationWorker(self.service)
        worker._recovery_needed = False
        self.channel.history.side_effect = lambda **kwargs: history(self.message)
        with patch.object(self.service, "safely_detect_message", return_value=False):
            await worker.recover_new_messages()
        self.assertNotIn(self.channel.id, worker._text_recovery_cursors)
        self.assertTrue(worker._recovery_needed)

    async def test_text_channels_enable_only_the_standard_message_intent(self) -> None:
        client = TemporaryPostBot(self.settings)
        try:
            self.assertTrue(client.intents.guilds)
            self.assertTrue(client.intents.guild_messages)
            self.assertFalse(client.intents.message_content)
            self.assertFalse(client.intents.members)
            self.assertFalse(client.intents.dm_messages)
        finally:
            await client.close()

    async def test_removed_text_channel_pauses_saved_schedule(self) -> None:
        prompt_id = await self.schedule()
        self.service.settings = Settings(
            discord_token="test-token", forum_channel_ids=frozenset({30}),
            database_path=self.settings.database_path,
        )
        await self.service.delete_expired(self.message.id, self.now + 300)
        self.partial(self.message.id).delete.assert_not_awaited()
        self.partial(prompt_id).delete.assert_not_awaited()
