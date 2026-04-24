from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
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
                    first_name TEXT NOT NULL
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
                """
            )

    def upsert_user(self, user_id: int, username: str | None, first_name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name),
            )

    def record_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        username: str | None,
        first_name: str,
        day: date,
        is_forward_public: bool,
        is_video_note: bool,
    ) -> None:
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name)
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

    def increment_mentions(self, user_ids: list[int], day: date) -> None:
        if not user_ids:
            return
        with self.connect() as conn:
            for user_id in user_ids:
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
        user_id: int,
        username: str | None,
        first_name: str,
        day: date,
        delta: int,
    ) -> None:
        if delta == 0:
            return
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name)
            self._ensure_activity_row(conn, user_id, day.isoformat())
            conn.execute(
                """
                UPDATE activity
                SET reactions_given = reactions_given + ?
                WHERE user_id = ? AND date = ?
                """,
                (delta, user_id, day.isoformat()),
            )

    def apply_reaction_delta(self, *, chat_id: int, message_id: int, delta: int) -> bool:
        if delta == 0:
            return False

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, message_date, reactions_received
                FROM message_stats
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return False

            self._ensure_activity_row(conn, row["user_id"], row["message_date"])
            conn.execute(
                """
                UPDATE activity
                SET reactions_received = reactions_received + ?
                WHERE user_id = ? AND date = ?
                """,
                (delta, row["user_id"], row["message_date"]),
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
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    u.first_name,
                    COALESCE(SUM(a.messages), 0) AS messages,
                    COALESCE(SUM(a.reactions_received), 0) AS reactions_received,
                    COALESCE(SUM(a.reactions_given), 0) AS reactions_given,
                    COALESCE(SUM(a.mentions), 0) AS mentions,
                    COALESCE(SUM(a.forwards_public), 0) AS forwards_public,
                    COALESCE(SUM(a.video_notes), 0) AS video_notes
                FROM users u
                LEFT JOIN activity a
                    ON a.user_id = u.user_id
                    AND a.date BETWEEN ? AND ?
                GROUP BY u.user_id, u.username, u.first_name
                ORDER BY u.user_id
                """,
                (start, end),
            ).fetchall()
        return [
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
            )
            for row in rows
        ]

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
    ) -> None:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
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
