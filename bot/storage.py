from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from bot.rating import UserWeekStats, week_key


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS activity (
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    messages INTEGER NOT NULL DEFAULT 0,
                    reactions_received INTEGER NOT NULL DEFAULT 0,
                    reactions_given INTEGER NOT NULL DEFAULT 0,
                    mentions INTEGER NOT NULL DEFAULT 0,
                    forwards_public INTEGER NOT NULL DEFAULT 0,
                    video_notes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, date),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                );

                CREATE TABLE IF NOT EXISTS titles (
                    user_id INTEGER NOT NULL,
                    week TEXT NOT NULL,
                    title TEXT NOT NULL,
                    PRIMARY KEY (week, title),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                );

                CREATE TABLE IF NOT EXISTS message_stats (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_date TEXT NOT NULL,
                    reactions_received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS weekly_reports (
                    week TEXT PRIMARY KEY,
                    posted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rep_votes (
                    chat_id INTEGER NOT NULL,
                    week TEXT NOT NULL,
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    value INTEGER NOT NULL CHECK(value IN (-1, 1)),
                    voted_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, week, from_user_id, to_user_id)
                );

                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS message_content (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_date TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text_content TEXT,
                    reply_to_message_id INTEGER,
                    file_id TEXT,
                    transcript_text TEXT,
                    transcript_source TEXT NOT NULL DEFAULT 'none',
                    transcript_status TEXT NOT NULL DEFAULT 'not_needed',
                    PRIMARY KEY (chat_id, message_id)
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "is_bot" not in columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0"
                )

    def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str,
        is_bot: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, is_bot)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    is_bot = excluded.is_bot
                """,
                (user_id, username, first_name, int(is_bot)),
            )

    def register_chat_presence(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        first_name: str,
        is_bot: bool,
        seen_at: date,
    ) -> None:
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name, is_bot)
            self._touch_chat_member(conn, chat_id, user_id, seen_at.isoformat())

    def record_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        username: str | None,
        first_name: str,
        is_bot: bool,
        day: date,
        is_forward_public: bool,
        is_video_note: bool,
    ) -> None:
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name, is_bot)
            self._touch_chat_member(conn, chat_id, user_id, day.isoformat())
            self._ensure_activity_row(conn, user_id, day.isoformat())
            conn.execute(
                """
                UPDATE activity
                SET messages = messages + 1,
                    forwards_public = forwards_public + ?,
                    video_notes = video_notes + ?
                WHERE user_id = ? AND date = ?
                """,
                (1 if is_forward_public else 0, 1 if is_video_note else 0, user_id, day.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO message_stats (chat_id, message_id, user_id, message_date, reactions_received)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(chat_id, message_id) DO NOTHING
                """,
                (chat_id, message_id, user_id, day.isoformat()),
            )

    def save_message_content(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        message_date: date,
        message_type: str,
        text_content: str | None,
        reply_to_message_id: int | None,
        file_id: str | None,
        transcript_status: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO message_content (
                    chat_id, message_id, user_id, message_date, message_type,
                    text_content, reply_to_message_id, file_id, transcript_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    message_date = excluded.message_date,
                    message_type = excluded.message_type,
                    text_content = excluded.text_content,
                    reply_to_message_id = excluded.reply_to_message_id,
                    file_id = excluded.file_id,
                    transcript_status = excluded.transcript_status
                """,
                (
                    chat_id,
                    message_id,
                    user_id,
                    message_date.isoformat(),
                    message_type,
                    text_content,
                    reply_to_message_id,
                    file_id,
                    transcript_status,
                ),
            )

    def save_salute_transcript(
        self,
        *,
        chat_id: int,
        reply_to_message_id: int,
        transcript_text: str,
    ) -> bool:
        if not transcript_text.strip():
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM message_content
                WHERE chat_id = ?
                  AND message_id = ?
                  AND message_type IN ('voice', 'video_note')
                """,
                (chat_id, reply_to_message_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE message_content
                SET transcript_text = ?,
                    transcript_source = 'salute',
                    transcript_status = 'done'
                WHERE chat_id = ? AND message_id = ?
                """,
                (transcript_text.strip(), chat_id, reply_to_message_id),
            )
            return True

    def get_recent_message_content(self, *, chat_id: int, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT mc.*, u.username, u.first_name, u.is_bot
                FROM message_content mc
                JOIN users u ON u.user_id = mc.user_id
                WHERE mc.chat_id = ?
                ORDER BY mc.message_date DESC, mc.message_id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def increment_mentions(self, *, chat_id: int, user_ids: list[int], day: date) -> None:
        if not user_ids:
            return
        with self.connect() as conn:
            for user_id in user_ids:
                self._touch_chat_member(conn, chat_id, user_id, day.isoformat())
                self._ensure_activity_row(conn, user_id, day.isoformat())
                conn.execute(
                    """
                    UPDATE activity
                    SET mentions = mentions + 1
                    WHERE user_id = ? AND date = ?
                    """,
                    (user_id, day.isoformat()),
                )

    def increment_reactions_given(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        first_name: str,
        is_bot: bool,
        day: date,
        delta: int,
    ) -> None:
        if delta == 0:
            return
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name, is_bot)
            self._touch_chat_member(conn, chat_id, user_id, day.isoformat())
            self._ensure_activity_row(conn, user_id, day.isoformat())
            conn.execute(
                """
                UPDATE activity
                SET reactions_given = reactions_given + ?
                WHERE user_id = ? AND date = ?
                """,
                (delta, user_id, day.isoformat()),
            )

    def apply_reaction_delta(
        self,
        *,
        chat_id: int,
        message_id: int,
        day: date,
        delta: int,
    ) -> bool:
        if delta == 0:
            return False

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT user_id
                FROM message_stats
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return False

            self._touch_chat_member(conn, chat_id, row["user_id"], day.isoformat())
            self._ensure_activity_row(conn, row["user_id"], day.isoformat())
            conn.execute(
                """
                UPDATE activity
                SET reactions_received = reactions_received + ?
                WHERE user_id = ? AND date = ?
                """,
                (delta, row["user_id"], day.isoformat()),
            )
            conn.execute(
                """
                UPDATE message_stats
                SET reactions_received = reactions_received + ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (delta, chat_id, message_id),
            )
            return True

    def find_user_ids_by_usernames(self, usernames: list[str]) -> dict[str, int]:
        if not usernames:
            return {}

        placeholders = ",".join("?" for _ in usernames)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT user_id, username
                FROM users
                WHERE username IN ({placeholders})
                """,
                usernames,
            ).fetchall()
        return {row["username"]: row["user_id"] for row in rows if row["username"]}

    def get_week_stats(self, week_start: date) -> list[UserWeekStats]:
        start = week_start.isoformat()
        end = (week_start + timedelta(days=6)).isoformat()
        week = week_key(week_start)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    u.first_name,
                    COALESCE(a.messages, 0) AS messages,
                    COALESCE(a.reactions_received, 0) AS reactions_received,
                    COALESCE(a.reactions_given, 0) AS reactions_given,
                    COALESCE(a.mentions, 0) AS mentions,
                    COALESCE(a.forwards_public, 0) AS forwards_public,
                    COALESCE(a.video_notes, 0) AS video_notes,
                    COALESCE(r.rep_plus, 0) AS rep_plus,
                    COALESCE(r.rep_minus, 0) AS rep_minus
                FROM users u
                LEFT JOIN (
                    SELECT
                        user_id,
                        SUM(messages) AS messages,
                        SUM(reactions_received) AS reactions_received,
                        SUM(reactions_given) AS reactions_given,
                        SUM(mentions) AS mentions,
                        SUM(forwards_public) AS forwards_public,
                        SUM(video_notes) AS video_notes
                    FROM activity
                    WHERE date BETWEEN ? AND ?
                    GROUP BY user_id
                ) a ON a.user_id = u.user_id
                LEFT JOIN (
                    SELECT
                        to_user_id AS user_id,
                        SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END) AS rep_plus,
                        SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS rep_minus
                    FROM rep_votes
                    WHERE week = ?
                    GROUP BY to_user_id
                ) r ON r.user_id = u.user_id
                ORDER BY u.user_id
                """,
                (start, end, week),
            ).fetchall()
        stats = [
            UserWeekStats(
                user_id=row["user_id"],
                username=row["username"],
                first_name=row["first_name"],
                messages=row["messages"],
                reactions_received=row["reactions_received"],
                reactions_given=row["reactions_given"],
                mentions=row["mentions"],
                forwards_public=row["forwards_public"],
                video_notes=row["video_notes"],
                rep_plus=row["rep_plus"],
                rep_minus=row["rep_minus"],
            )
            for row in rows
        ]
        return [item for item in stats if self._has_week_activity(item)]

    def list_rep_candidates(self, *, chat_id: int, exclude_user_id: int) -> list[SimpleNamespace]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.user_id, u.username, u.first_name
                FROM chat_members cm
                JOIN users u ON u.user_id = cm.user_id
                WHERE cm.chat_id = ?
                  AND u.user_id != ?
                  AND u.is_bot = 0
                ORDER BY
                    CASE WHEN username IS NULL OR username = '' THEN 1 ELSE 0 END,
                    LOWER(COALESCE(username, first_name)),
                    u.user_id
                """,
                (chat_id, exclude_user_id),
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO chat_members (chat_id, user_id, first_seen_at, last_seen_at)
                    SELECT ?, user_id, DATE('now'), DATE('now')
                    FROM users
                    WHERE user_id != ?
                      AND is_bot = 0
                    """,
                    (chat_id, exclude_user_id),
                )
                rows = conn.execute(
                    """
                    SELECT u.user_id, u.username, u.first_name
                    FROM chat_members cm
                    JOIN users u ON u.user_id = cm.user_id
                    WHERE cm.chat_id = ?
                      AND u.user_id != ?
                      AND u.is_bot = 0
                    ORDER BY
                        CASE WHEN username IS NULL OR username = '' THEN 1 ELSE 0 END,
                        LOWER(COALESCE(username, first_name)),
                        u.user_id
                    """,
                    (chat_id, exclude_user_id),
                ).fetchall()
        return [
            SimpleNamespace(
                user_id=row["user_id"],
                username=row["username"],
                first_name=row["first_name"],
            )
            for row in rows
        ]

    def apply_rep_vote(
        self,
        *,
        chat_id: int,
        week_start: date,
        from_user_id: int,
        from_username: str | None,
        from_first_name: str,
        from_is_bot: bool,
        to_user_id: int,
        to_username: str | None,
        to_first_name: str,
        to_is_bot: bool,
        value: int,
        voted_at: datetime,
    ) -> str:
        week = week_key(week_start)
        with self.connect() as conn:
            self._upsert_user(
                conn, from_user_id, from_username, from_first_name, from_is_bot
            )
            self._upsert_user(conn, to_user_id, to_username, to_first_name, to_is_bot)
            self._touch_chat_member(conn, chat_id, from_user_id, voted_at.date().isoformat())
            self._touch_chat_member(conn, chat_id, to_user_id, voted_at.date().isoformat())
            row = conn.execute(
                """
                SELECT value
                FROM rep_votes
                WHERE chat_id = ? AND week = ? AND from_user_id = ? AND to_user_id = ?
                """,
                (chat_id, week, from_user_id, to_user_id),
            ).fetchone()
            if row is not None and row["value"] == value:
                return "unchanged"

            conn.execute(
                """
                INSERT INTO rep_votes (chat_id, week, from_user_id, to_user_id, value, voted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, week, from_user_id, to_user_id) DO UPDATE SET
                    value = excluded.value,
                    voted_at = excluded.voted_at
                """,
                (
                    chat_id,
                    week,
                    from_user_id,
                    to_user_id,
                    value,
                    voted_at.isoformat(),
                ),
            )
            return "created" if row is None else "flipped"

    def save_titles(self, week_start: date, title_pairs: list[tuple[int, str]]) -> None:
        week = week_key(week_start)
        with self.connect() as conn:
            for user_id, title in title_pairs:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO titles (user_id, week, title)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, week, title),
                )

    def get_titles_for_user(self, week_start: date, user_id: int) -> list[str]:
        week = week_key(week_start)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT title
                FROM titles
                WHERE week = ? AND user_id = ?
                ORDER BY title
                """,
                (week, user_id),
            ).fetchall()
        return [row["title"] for row in rows]

    def get_active_chat_id(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = 'active_chat_id'"
            ).fetchone()
        return int(row["value"]) if row else None

    def set_active_chat_id(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bot_state (key, value)
                VALUES ('active_chat_id', ?)
                """,
                (str(chat_id),),
            )

    def report_already_posted(self, week_start: date) -> bool:
        week = week_key(week_start)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM weekly_reports WHERE week = ?",
                (week,),
            ).fetchone()
        return row is not None

    def mark_report_posted(self, week_start: date, posted_at: datetime) -> None:
        week = week_key(week_start)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO weekly_reports (week, posted_at)
                VALUES (?, ?)
                """,
                (week, posted_at.isoformat()),
            )

    def _upsert_user(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        username: str | None,
        first_name: str,
        is_bot: bool = False,
    ) -> None:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, is_bot)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_bot = excluded.is_bot
            """,
            (user_id, username, first_name, int(is_bot)),
        )

    def _ensure_activity_row(self, conn: sqlite3.Connection, user_id: int, day: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO activity (
                user_id, date, messages, reactions_received, reactions_given,
                mentions, forwards_public, video_notes
            )
            VALUES (?, ?, 0, 0, 0, 0, 0, 0)
            """,
            (user_id, day),
        )

    def _touch_chat_member(
        self,
        conn: sqlite3.Connection,
        chat_id: int,
        user_id: int,
        seen_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO chat_members (chat_id, user_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at
            """,
            (chat_id, user_id, seen_at, seen_at),
        )

    def _has_week_activity(self, item: UserWeekStats) -> bool:
        return any(
            (
                item.messages,
                item.reactions_received,
                item.reactions_given,
                item.mentions,
                item.forwards_public,
                item.video_notes,
                item.rep_plus,
                item.rep_minus,
            )
        )
