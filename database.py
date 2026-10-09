from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)
PostStatus = Literal["pending_author", "awaiting_selection", "scheduled", "deleted", "ignored"]


@dataclass(frozen=True, slots=True)
class Post:
    thread_id: int
    guild_id: int
    forum_channel_id: int
    author_id: int | None
    created_at: int
    expires_at: int | None
    selection_deadline: int
    default_expires_at: int
    status: PostStatus
    prompt_message_id: int | None
    default_applied: bool
    needs_sync: bool
    deleted_at: int | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Post:
        values = dict(row)
        values["default_applied"] = bool(values["default_applied"])
        values["needs_sync"] = bool(values["needs_sync"])
        return cls(**values)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been opened.")
        return self._connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = FULL;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS posts (
                thread_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                author_id INTEGER,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                selection_deadline INTEGER NOT NULL,
                default_expires_at INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'pending_author', 'awaiting_selection', 'scheduled', 'deleted', 'ignored'
                )),
                prompt_message_id INTEGER,
                default_applied INTEGER NOT NULL DEFAULT 0,
                needs_sync INTEGER NOT NULL DEFAULT 1,
                deleted_at INTEGER,
                CHECK (status != 'scheduled' OR expires_at IS NOT NULL),
                CHECK (status NOT IN ('awaiting_selection', 'scheduled') OR author_id IS NOT NULL)
            );
            CREATE INDEX IF NOT EXISTS posts_expiration ON posts(status, expires_at);
            CREATE INDEX IF NOT EXISTS posts_selection ON posts(status, selection_deadline);
            CREATE INDEX IF NOT EXISTS posts_sync ON posts(needs_sync, status);
        """)
        await self.execute("UPDATE posts SET needs_sync = 1 WHERE status = 'awaiting_selection'")
        logger.info("SQLite initialized at %s", self.path)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        async with self._lock:
            async with self.connection.execute(sql, parameters) as cursor:
                return cursor.rowcount

    async def _posts(self, sql: str, parameters: tuple[object, ...] = ()) -> list[Post]:
        async with self._lock:
            async with self.connection.execute(sql, parameters) as cursor:
                rows = await cursor.fetchall()
        return [Post.from_row(row) for row in rows]

    async def get_post(self, thread_id: int) -> Post | None:
        posts = await self._posts("SELECT * FROM posts WHERE thread_id = ?", (thread_id,))
        return posts[0] if posts else None

    async def record_thread(
        self,
        *,
        thread_id: int,
        guild_id: int,
        forum_channel_id: int,
        author_id: int | None,
        created_at: int,
        selection_timeout: int,
        default_lifetime: int,
    ) -> bool:
        changed = await self.execute(
            """INSERT INTO posts (
                thread_id, guild_id, forum_channel_id, author_id, created_at,
                selection_deadline, default_expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_author')
            ON CONFLICT(thread_id) DO NOTHING""",
            (thread_id, guild_id, forum_channel_id, author_id, created_at,
             created_at + selection_timeout, created_at + default_lifetime),
        )
        return changed == 1

    async def resolve_author(self, thread_id: int, author_id: int, *, is_bot: bool) -> None:
        await self.execute(
            """UPDATE posts SET author_id = ?, status = ?, needs_sync = ?
            WHERE thread_id = ? AND status = 'pending_author'""",
            (author_id, "ignored" if is_bot else "awaiting_selection", int(not is_bot), thread_id),
        )

    async def select_lifetime(self, thread_id: int, author_id: int, seconds: int, now: int) -> bool:
        changed = await self.execute(
            """UPDATE posts SET status = 'scheduled', expires_at = created_at + ?,
            default_applied = 0, needs_sync = 1
            WHERE thread_id = ? AND author_id = ? AND status = 'awaiting_selection'
            AND selection_deadline > ?""",
            (seconds, thread_id, author_id, now),
        )
        return changed == 1

    async def apply_default(self, thread_id: int, now: int) -> bool:
        changed = await self.execute(
            """UPDATE posts SET status = 'scheduled', expires_at = default_expires_at,
            default_applied = 1, needs_sync = 1
            WHERE thread_id = ? AND status = 'awaiting_selection' AND selection_deadline <= ?""",
            (thread_id, now),
        )
        return changed == 1

    async def overdue_selections(self, now: int) -> list[Post]:
        return await self._posts(
            "SELECT * FROM posts WHERE status = 'awaiting_selection' AND selection_deadline <= ?",
            (now,),
        )

    async def expired_posts(self, now: int) -> list[Post]:
        return await self._posts(
            "SELECT * FROM posts WHERE status = 'scheduled' AND expires_at <= ? ORDER BY expires_at",
            (now,),
        )

    async def posts_to_sync(self) -> list[Post]:
        return await self._posts(
            """SELECT * FROM posts WHERE status = 'pending_author'
            OR (needs_sync = 1 AND status IN ('awaiting_selection', 'scheduled'))"""
        )

    async def set_prompt(self, thread_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE posts SET prompt_message_id = ? WHERE thread_id = ?",
            (message_id, thread_id),
        )

    async def mark_synced(self, thread_id: int) -> None:
        await self.execute("UPDATE posts SET needs_sync = 0 WHERE thread_id = ?", (thread_id,))

    async def mark_deleted(self, thread_id: int, now: int) -> None:
        await self.execute(
            """UPDATE posts SET status = 'deleted', deleted_at = ?, needs_sync = 0
            WHERE thread_id = ? AND status NOT IN ('deleted', 'ignored')""",
            (now, thread_id),
        )
