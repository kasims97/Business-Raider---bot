from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from bot.stats import PlayerTotals, merge_totals

PENDING_INPUT_TTL = timedelta(minutes=10)


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

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS tournaments (
                    tournament_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    game TEXT,
                    team_mode INTEGER,
                    status TEXT NOT NULL DEFAULT 'setup',
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tournament_players (
                    tournament_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    team INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tournament_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS matches (
                    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    match_no INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'live',
                    created_at TEXT NOT NULL,
                    winner_team INTEGER
                );

                CREATE TABLE IF NOT EXISTS match_players (
                    match_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    team INTEGER NOT NULL DEFAULT 0,
                    top INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (match_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS kills (
                    kill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    killer_id INTEGER NOT NULL,
                    victim_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS predictions (
                    tournament_id INTEGER NOT NULL,
                    voter_id INTEGER NOT NULL,
                    pick_user_id INTEGER NOT NULL,
                    PRIMARY KEY (tournament_id, voter_id)
                );

                CREATE TABLE IF NOT EXISTS mvp_votes (
                    tournament_id INTEGER NOT NULL,
                    voter_id INTEGER NOT NULL,
                    pick_user_id INTEGER NOT NULL,
                    PRIMARY KEY (tournament_id, voter_id)
                );

                CREATE TABLE IF NOT EXISTS pending_input (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                """
            )

    # -- users / presence -------------------------------------------------

    def upsert_user(self, user_id: int, username: str | None, first_name: str, is_bot: bool = False) -> None:
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name, is_bot)

    def register_chat_presence(
        self, *, chat_id: int, user_id: int, username: str | None, first_name: str, is_bot: bool, seen_at: date
    ) -> None:
        if is_bot:
            return
        with self.connect() as conn:
            self._upsert_user(conn, user_id, username, first_name, is_bot)
            self._touch_chat_member(conn, chat_id, user_id, seen_at.isoformat())

    def list_players(self, chat_id: int) -> list[SimpleNamespace]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.user_id, u.username, u.first_name
                FROM chat_members cm
                JOIN users u ON u.user_id = cm.user_id
                WHERE cm.chat_id = ? AND u.is_bot = 0
                ORDER BY
                    CASE WHEN username IS NULL OR username = '' THEN 1 ELSE 0 END,
                    LOWER(COALESCE(u.username, u.first_name)),
                    u.user_id
                """,
                (chat_id,),
            ).fetchall()
        return [
            SimpleNamespace(user_id=r["user_id"], username=r["username"], first_name=r["first_name"])
            for r in rows
        ]

    def add_guest_player(self, *, chat_id: int, first_name: str, seen_at: date) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(user_id) AS min_id FROM users WHERE user_id < 0").fetchone()
            next_id = (row["min_id"] - 1) if row and row["min_id"] is not None else -1
            self._upsert_user(conn, next_id, None, first_name, False)
            self._touch_chat_member(conn, chat_id, next_id, seen_at.isoformat())
        return next_id

    def remove_player(self, *, chat_id: int, user_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
            return cur.rowcount > 0

    # -- pending text input -------------------------------------------------

    def set_pending_input(self, *, chat_id: int, user_id: int, kind: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_input (chat_id, user_id, kind, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET kind = excluded.kind, created_at = excluded.created_at
                """,
                (chat_id, user_id, kind, datetime.now(timezone.utc).isoformat()),
            )

    def pop_pending_input(self, *, chat_id: int, user_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT kind, created_at FROM pending_input WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM pending_input WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
        created_at = datetime.fromisoformat(row["created_at"])
        if datetime.now(timezone.utc) - created_at > PENDING_INPUT_TTL:
            return None
        return row["kind"]

    # -- tournament setup ----------------------------------------------------

    def get_setup_draft(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tournaments WHERE chat_id = ? AND status = 'setup'", (chat_id,)
            ).fetchone()

    def get_active_tournament(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tournaments WHERE chat_id = ? AND status = 'active'", (chat_id,)
            ).fetchone()

    def get_tournament(self, tournament_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tournaments WHERE tournament_id = ?", (tournament_id,)
            ).fetchone()

    def create_setup_draft(self, *, chat_id: int, created_by: int) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT tournament_id FROM tournaments WHERE chat_id = ? AND status = 'setup'",
                (chat_id,),
            ).fetchone()
            if existing:
                return existing["tournament_id"]
            cur = conn.execute(
                """
                INSERT INTO tournaments (chat_id, name, status, created_by, created_at)
                VALUES (?, '', 'setup', ?, ?)
                """,
                (chat_id, created_by, datetime.now(timezone.utc).isoformat()),
            )
            return cur.lastrowid

    def set_draft_name(self, tournament_id: int, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tournaments SET name = ? WHERE tournament_id = ?", (name, tournament_id)
            )

    def set_draft_game(self, tournament_id: int, game: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tournaments SET game = ? WHERE tournament_id = ?", (game, tournament_id)
            )

    def set_draft_mode(self, tournament_id: int, team_mode: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tournaments SET team_mode = ? WHERE tournament_id = ?",
                (int(team_mode), tournament_id),
            )

    def toggle_draft_player(self, tournament_id: int, user_id: int, team_mode: bool) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT team FROM tournament_players WHERE tournament_id = ? AND user_id = ?",
                (tournament_id, user_id),
            ).fetchone()
            if not team_mode:
                if row is None:
                    conn.execute(
                        "INSERT INTO tournament_players (tournament_id, user_id, team) VALUES (?, ?, 0)",
                        (tournament_id, user_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM tournament_players WHERE tournament_id = ? AND user_id = ?",
                        (tournament_id, user_id),
                    )
                return

            if row is None:
                conn.execute(
                    "INSERT INTO tournament_players (tournament_id, user_id, team) VALUES (?, ?, 1)",
                    (tournament_id, user_id),
                )
            elif row["team"] == 1:
                conn.execute(
                    "UPDATE tournament_players SET team = 2 WHERE tournament_id = ? AND user_id = ?",
                    (tournament_id, user_id),
                )
            else:
                conn.execute(
                    "DELETE FROM tournament_players WHERE tournament_id = ? AND user_id = ?",
                    (tournament_id, user_id),
                )

    def get_draft_players(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT tp.user_id, tp.team, u.first_name, u.username
                FROM tournament_players tp
                JOIN users u ON u.user_id = tp.user_id
                WHERE tp.tournament_id = ?
                ORDER BY u.first_name
                """,
                (tournament_id,),
            ).fetchall()

    def cancel_draft(self, tournament_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM tournament_players WHERE tournament_id = ?", (tournament_id,))
            conn.execute("DELETE FROM tournaments WHERE tournament_id = ?", (tournament_id,))

    def start_tournament(self, tournament_id: int) -> int:
        """Activates the draft and opens match #1. Returns the new match_id."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE tournaments SET status = 'active' WHERE tournament_id = ?", (tournament_id,)
            )
            return self._open_next_match(conn, tournament_id)

    # -- live match ------------------------------------------------------

    def get_live_match(self, tournament_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? AND status = 'live'",
                (tournament_id,),
            ).fetchone()

    def get_match(self, match_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()

    def get_match_players(self, match_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT mp.user_id, mp.team, mp.top, u.first_name, u.username,
                       COALESCE((SELECT COUNT(*) FROM kills k WHERE k.match_id = mp.match_id AND k.killer_id = mp.user_id), 0) AS kills
                FROM match_players mp
                JOIN users u ON u.user_id = mp.user_id
                WHERE mp.match_id = ?
                ORDER BY u.first_name
                """,
                (match_id,),
            ).fetchall()

    def toggle_match_roster(self, match_id: int, user_id: int, team_mode: bool) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT team FROM match_players WHERE match_id = ? AND user_id = ?",
                (match_id, user_id),
            ).fetchone()
            if not team_mode:
                if row is None:
                    conn.execute(
                        "INSERT INTO match_players (match_id, user_id, team, top) VALUES (?, ?, 0, 0)",
                        (match_id, user_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM match_players WHERE match_id = ? AND user_id = ?",
                        (match_id, user_id),
                    )
                return

            if row is None:
                conn.execute(
                    "INSERT INTO match_players (match_id, user_id, team, top) VALUES (?, ?, 1, 0)",
                    (match_id, user_id),
                )
            elif row["team"] == 1:
                conn.execute(
                    "UPDATE match_players SET team = 2 WHERE match_id = ? AND user_id = ?",
                    (match_id, user_id),
                )
            else:
                conn.execute(
                    "DELETE FROM match_players WHERE match_id = ? AND user_id = ?",
                    (match_id, user_id),
                )

    def record_kill(self, *, match_id: int, killer_id: int, victim_id: int | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kills (match_id, killer_id, victim_id, created_at) VALUES (?, ?, ?, ?)",
                (match_id, killer_id, victim_id, datetime.now(timezone.utc).isoformat()),
            )

    def undo_last_kill(self, *, match_id: int, killer_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT kill_id FROM kills
                WHERE match_id = ? AND killer_id = ?
                ORDER BY kill_id DESC LIMIT 1
                """,
                (match_id, killer_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM kills WHERE kill_id = ?", (row["kill_id"],))
            return True

    def toggle_top(self, *, match_id: int, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE match_players SET top = 1 - top WHERE match_id = ? AND user_id = ?",
                (match_id, user_id),
            )

    def set_winner_team(self, *, match_id: int, team: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE matches SET winner_team = ? WHERE match_id = ?", (team, match_id)
            )

    def save_match_and_advance(self, *, tournament_id: int, match_id: int) -> int:
        """Marks the match saved, syncs the roster, opens the next match. Returns new match_id."""
        with self.connect() as conn:
            conn.execute("UPDATE matches SET status = 'saved' WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM tournament_players WHERE tournament_id = ?", (tournament_id,))
            conn.execute(
                """
                INSERT INTO tournament_players (tournament_id, user_id, team)
                SELECT ?, user_id, team FROM match_players WHERE match_id = ?
                """,
                (tournament_id, match_id),
            )
            return self._open_next_match(conn, tournament_id)

    def undo_last_saved_match(self, tournament_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT match_id FROM matches
                WHERE tournament_id = ? AND status = 'saved'
                ORDER BY match_no DESC LIMIT 1
                """,
                (tournament_id,),
            ).fetchone()
            if row is None:
                return False
            match_id = row["match_id"]
            conn.execute("DELETE FROM kills WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM matches WHERE match_id = ?", (match_id,))
            return True

    def finish_tournament(self, tournament_id: int) -> None:
        with self.connect() as conn:
            live = conn.execute(
                "SELECT match_id FROM matches WHERE tournament_id = ? AND status = 'live'",
                (tournament_id,),
            ).fetchone()
            if live is not None:
                conn.execute("DELETE FROM kills WHERE match_id = ?", (live["match_id"],))
                conn.execute("DELETE FROM match_players WHERE match_id = ?", (live["match_id"],))
                conn.execute("DELETE FROM matches WHERE match_id = ?", (live["match_id"],))
            conn.execute(
                "UPDATE tournaments SET status = 'finished', finished_at = ? WHERE tournament_id = ?",
                (datetime.now(timezone.utc).isoformat(), tournament_id),
            )

    def _open_next_match(self, conn: sqlite3.Connection, tournament_id: int) -> int:
        last_no = conn.execute(
            "SELECT COALESCE(MAX(match_no), 0) AS n FROM matches WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            """
            INSERT INTO matches (tournament_id, match_no, status, created_at)
            VALUES (?, ?, 'live', ?)
            """,
            (tournament_id, last_no + 1, datetime.now(timezone.utc).isoformat()),
        )
        match_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO match_players (match_id, user_id, team, top)
            SELECT ?, user_id, team, 0 FROM tournament_players WHERE tournament_id = ?
            """,
            (match_id, tournament_id),
        )
        return match_id

    # -- aggregation -------------------------------------------------------

    def get_tournament_totals(self, tournament_id: int) -> list[PlayerTotals]:
        with self.connect() as conn:
            matches = conn.execute(
                "SELECT match_id, winner_team FROM matches WHERE tournament_id = ? AND status = 'saved'",
                (tournament_id,),
            ).fetchall()
            match_ids = [m["match_id"] for m in matches]
            if not match_ids:
                return []
            winner_by_match = {m["match_id"]: m["winner_team"] for m in matches}
            placeholders = ",".join("?" for _ in match_ids)
            mp_rows = conn.execute(
                f"SELECT match_id, user_id, team, top FROM match_players WHERE match_id IN ({placeholders})",
                match_ids,
            ).fetchall()
            kill_rows = conn.execute(
                f"SELECT killer_id, COUNT(*) AS c FROM kills WHERE match_id IN ({placeholders}) GROUP BY killer_id",
                match_ids,
            ).fetchall()
            users = {
                u["user_id"]: u for u in conn.execute("SELECT user_id, username, first_name FROM users").fetchall()
            }

        kills_by_user = {r["killer_id"]: r["c"] for r in kill_rows}
        played: Counter[int] = Counter()
        tops: Counter[int] = Counter()
        for row in mp_rows:
            played[row["user_id"]] += 1
            if row["team"] in (1, 2):
                won = row["team"] == winner_by_match[row["match_id"]]
            else:
                won = row["top"] == 1
            if won:
                tops[row["user_id"]] += 1

        totals = []
        for user_id in played:
            user = users.get(user_id)
            if user is None:
                continue
            totals.append(
                PlayerTotals(
                    user_id=user_id,
                    username=user["username"],
                    first_name=user["first_name"],
                    played=played[user_id],
                    kills=kills_by_user.get(user_id, 0),
                    tops=tops[user_id],
                )
            )
        return totals

    def get_match_kills(self, match_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT k.killer_id, ku.first_name AS killer_name,
                       k.victim_id, vu.first_name AS victim_name
                FROM kills k
                JOIN users ku ON ku.user_id = k.killer_id
                LEFT JOIN users vu ON vu.user_id = k.victim_id
                WHERE k.match_id = ?
                ORDER BY k.kill_id
                """,
                (match_id,),
            ).fetchall()

    def get_kill_matrix(self, tournament_id: int) -> dict[tuple[int, int], int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT k.killer_id, k.victim_id, COUNT(*) AS c
                FROM kills k
                JOIN matches m ON m.match_id = k.match_id
                WHERE m.tournament_id = ? AND m.status = 'saved' AND k.victim_id IS NOT NULL
                GROUP BY k.killer_id, k.victim_id
                """,
                (tournament_id,),
            ).fetchall()
        return {(r["killer_id"], r["victim_id"]): r["c"] for r in rows}

    def get_match_results_sequence(self, tournament_id: int) -> list[dict[int, bool]]:
        with self.connect() as conn:
            matches = conn.execute(
                """
                SELECT match_id, winner_team FROM matches
                WHERE tournament_id = ? AND status = 'saved'
                ORDER BY match_no
                """,
                (tournament_id,),
            ).fetchall()
            result = []
            for m in matches:
                rows = conn.execute(
                    "SELECT user_id, team, top FROM match_players WHERE match_id = ?",
                    (m["match_id"],),
                ).fetchall()
                entry = {}
                for row in rows:
                    if row["team"] in (1, 2):
                        entry[row["user_id"]] = row["team"] == m["winner_team"]
                    else:
                        entry[row["user_id"]] = row["top"] == 1
                result.append(entry)
        return result

    def get_league_totals(self, chat_id: int) -> list[PlayerTotals]:
        with self.connect() as conn:
            tournament_ids = [
                r["tournament_id"]
                for r in conn.execute(
                    "SELECT tournament_id FROM tournaments WHERE chat_id = ? AND status = 'finished'",
                    (chat_id,),
                ).fetchall()
            ]
        all_totals: list[PlayerTotals] = []
        for tournament_id in tournament_ids:
            all_totals.extend(self.get_tournament_totals(tournament_id))
        return merge_totals(all_totals)

    def list_finished_tournaments(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM tournaments
                WHERE chat_id = ? AND status = 'finished'
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

    def list_recent_matches(self, tournament_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM matches
                WHERE tournament_id = ? AND status = 'saved'
                ORDER BY match_no DESC
                LIMIT ?
                """,
                (tournament_id, limit),
            ).fetchall()

    # -- predictions / mvp -----------------------------------------------

    def cast_prediction(self, *, tournament_id: int, voter_id: int, pick_user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO predictions (tournament_id, voter_id, pick_user_id)
                VALUES (?, ?, ?)
                ON CONFLICT(tournament_id, voter_id) DO UPDATE SET pick_user_id = excluded.pick_user_id
                """,
                (tournament_id, voter_id, pick_user_id),
            )

    def has_any_saved_match(self, tournament_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM matches WHERE tournament_id = ? AND status = 'saved' LIMIT 1",
                (tournament_id,),
            ).fetchone()
        return row is not None

    def get_predictions(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT p.voter_id, p.pick_user_id, u.first_name AS voter_name, pu.first_name AS pick_name
                FROM predictions p
                JOIN users u ON u.user_id = p.voter_id
                JOIN users pu ON pu.user_id = p.pick_user_id
                WHERE p.tournament_id = ?
                """,
                (tournament_id,),
            ).fetchall()

    def cast_mvp_vote(self, *, tournament_id: int, voter_id: int, pick_user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mvp_votes (tournament_id, voter_id, pick_user_id)
                VALUES (?, ?, ?)
                ON CONFLICT(tournament_id, voter_id) DO UPDATE SET pick_user_id = excluded.pick_user_id
                """,
                (tournament_id, voter_id, pick_user_id),
            )

    def get_mvp_tally(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pu.user_id, pu.first_name, COUNT(*) AS votes
                FROM mvp_votes v
                JOIN users pu ON pu.user_id = v.pick_user_id
                WHERE v.tournament_id = ?
                GROUP BY pu.user_id
                ORDER BY votes DESC
                """,
                (tournament_id,),
            ).fetchall()

    # -- settings ----------------------------------------------------------

    def get_setting(self, chat_id: int, name: str, default: bool = True) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (f"setting:{chat_id}:{name}",)
            ).fetchone()
        if row is None:
            return default
        return row["value"] == "1"

    def toggle_setting(self, chat_id: int, name: str, default: bool = True) -> bool:
        new_value = not self.get_setting(chat_id, name, default)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                (f"setting:{chat_id}:{name}", "1" if new_value else "0"),
            )
        return new_value

    # -- internal ------------------------------------------------------------

    def _upsert_user(
        self, conn: sqlite3.Connection, user_id: int, username: str | None, first_name: str, is_bot: bool = False
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

    def _touch_chat_member(self, conn: sqlite3.Connection, chat_id: int, user_id: int, seen_at: str) -> None:
        conn.execute(
            """
            INSERT INTO chat_members (chat_id, user_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (chat_id, user_id, seen_at, seen_at),
        )
