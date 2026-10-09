from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "posts.sqlite3")
        await self.database.open()
        await self.database.record_thread(
            thread_id=10, guild_id=20, forum_channel_id=30, author_id=40,
            created_at=1000, selection_timeout=3600, default_lifetime=86400,
        )
        await self.database.resolve_author(10, 40, is_bot=False)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.directory.cleanup()

    async def test_concurrent_selections_have_one_winner(self) -> None:
        results = await asyncio.gather(
            self.database.select_lifetime(10, 40, 3600, 1001),
            self.database.select_lifetime(10, 40, 21600, 1001),
        )
        self.assertEqual(sum(results), 1)
        post = await self.database.get_post(10)
        self.assertEqual(post.status, "scheduled")
        self.assertIn(post.expires_at, (4600, 22600))

    async def test_wrong_author_cannot_select(self) -> None:
        self.assertFalse(await self.database.select_lifetime(10, 99, 3600, 1001))
        self.assertEqual((await self.database.get_post(10)).status, "awaiting_selection")

    async def test_timeout_boundary_and_default_use_creation_time(self) -> None:
        self.assertFalse(await self.database.select_lifetime(10, 40, 3600, 4600))
        self.assertTrue(await self.database.apply_default(10, 4600))
        self.assertFalse(await self.database.apply_default(10, 4601))
        post = await self.database.get_post(10)
        self.assertEqual(post.expires_at, 87400)
        self.assertTrue(post.default_applied)

    async def test_default_and_selection_race_does_not_overwrite(self) -> None:
        results = await asyncio.gather(
            self.database.select_lifetime(10, 40, 3600, 4600),
            self.database.apply_default(10, 4600),
        )
        self.assertEqual(results, [False, True])
        self.assertEqual((await self.database.get_post(10)).expires_at, 87400)

    async def test_schedule_and_pending_sync_survive_reopen(self) -> None:
        await self.database.set_prompt(10, 50)
        await self.database.select_lifetime(10, 40, 3600, 1001)
        await self.database.close()
        await self.database.open()
        post = await self.database.get_post(10)
        self.assertEqual(post.prompt_message_id, 50)
        self.assertEqual(post.expires_at, 4600)
        self.assertTrue(post.needs_sync)
        self.assertEqual(await self.database.expired_posts(4599), [])
        self.assertEqual(len(await self.database.expired_posts(4600)), 1)

    async def test_awaiting_prompt_refreshes_after_restart(self) -> None:
        await self.database.mark_synced(10)
        await self.database.close()
        await self.database.open()
        self.assertTrue((await self.database.get_post(10)).needs_sync)

    async def test_duplicate_detection_preserves_schedule(self) -> None:
        await self.database.select_lifetime(10, 40, 3600, 1001)
        inserted = await self.database.record_thread(
            thread_id=10, guild_id=20, forum_channel_id=30, author_id=99,
            created_at=2000, selection_timeout=1, default_lifetime=1,
        )
        self.assertFalse(inserted)
        post = await self.database.get_post(10)
        self.assertEqual(post.author_id, 40)
        self.assertEqual(post.expires_at, 4600)

    async def test_deleted_and_bot_threads_are_not_pending(self) -> None:
        await self.database.mark_deleted(10, 2000)
        self.assertEqual((await self.database.get_post(10)).deleted_at, 2000)
        await self.database.record_thread(
            thread_id=11, guild_id=20, forum_channel_id=30, author_id=41,
            created_at=1000, selection_timeout=3600, default_lifetime=86400,
        )
        await self.database.resolve_author(11, 41, is_bot=True)
        self.assertEqual((await self.database.get_post(11)).status, "ignored")
        self.assertEqual(await self.database.posts_to_sync(), [])
        self.assertEqual(await self.database.overdue_selections(999999), [])
