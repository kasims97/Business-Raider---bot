import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from bot.rating import (
    TITLE_USELESS,
    UserWeekStats,
    compute_score,
    pick_titles,
    sort_for_ranking,
    week_start_for_day,
    week_start_for_dt,
)
from bot.storage import Storage


class RatingTests(unittest.TestCase):
    def test_compute_score(self) -> None:
        self.assertEqual(
            compute_score(
                messages=10,
                reactions_received=2,
                reactions_given=4,
                mentions=3,
                rep_balance=2,
            ),
            28.0,
        )

    def test_week_start_uses_monday(self) -> None:
        self.assertEqual(week_start_for_day(date(2026, 4, 26)), date(2026, 4, 20))

    def test_week_start_datetime(self) -> None:
        tz = ZoneInfo("Europe/Moscow")
        moment = datetime(2026, 4, 26, 22, 0, tzinfo=tz)
        self.assertEqual(week_start_for_dt(moment), date(2026, 4, 20))

    def test_ranking_sorts_by_score_then_messages(self) -> None:
        stats = [
            UserWeekStats(user_id=2, username=None, first_name="B", messages=8, reactions_received=0),
            UserWeekStats(user_id=1, username=None, first_name="A", messages=8, reactions_received=0),
        ]
        ranked = sort_for_ranking(stats)
        self.assertEqual([item.user_id for item in ranked], [1, 2])

    def test_titles_include_useless(self) -> None:
        stats = [
            UserWeekStats(
                user_id=1,
                username="a",
                first_name="A",
                messages=10,
                reactions_received=5,
                forwards_public=2,
                video_notes=1,
                rep_plus=3,
                rep_minus=1,
            ),
            UserWeekStats(
                user_id=2,
                username="b",
                first_name="B",
                messages=1,
                reactions_received=0,
                forwards_public=0,
                video_notes=0,
                rep_minus=2,
            ),
        ]
        titles = dict((title, user_id) for user_id, title in pick_titles(stats))
        self.assertEqual(titles[TITLE_USELESS], 2)

    def test_apply_rep_vote_statuses_and_week_stats(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            week_start = date(2026, 4, 20)
            storage.record_message(
                chat_id=1,
                message_id=1,
                user_id=20,
                username="target",
                first_name="Target",
                is_bot=False,
                day=week_start,
                is_forward_public=False,
                is_video_note=False,
            )
            voted_at = datetime(2026, 4, 22, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))

            created = storage.apply_rep_vote(
                chat_id=1,
                week_start=week_start,
                from_user_id=10,
                from_username="giver",
                from_first_name="Giver",
                from_is_bot=False,
                to_user_id=20,
                to_username="target",
                to_first_name="Target",
                to_is_bot=False,
                value=1,
                voted_at=voted_at,
            )
            unchanged = storage.apply_rep_vote(
                chat_id=1,
                week_start=week_start,
                from_user_id=10,
                from_username="giver",
                from_first_name="Giver",
                from_is_bot=False,
                to_user_id=20,
                to_username="target",
                to_first_name="Target",
                to_is_bot=False,
                value=1,
                voted_at=voted_at,
            )
            flipped = storage.apply_rep_vote(
                chat_id=1,
                week_start=week_start,
                from_user_id=10,
                from_username="giver",
                from_first_name="Giver",
                from_is_bot=False,
                to_user_id=20,
                to_username="target",
                to_first_name="Target",
                to_is_bot=False,
                value=-1,
                voted_at=voted_at,
            )

            self.assertEqual(created, "created")
            self.assertEqual(unchanged, "unchanged")
            self.assertEqual(flipped, "flipped")

            stats = {item.user_id: item for item in storage.get_week_stats(1, week_start)}
            self.assertEqual(stats[20].rep_plus, 0)
            self.assertEqual(stats[20].rep_minus, 1)
            self.assertEqual(stats[20].rep_balance, -1)

    def test_rep_candidates_exclude_self_and_bots(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            storage.upsert_user(1, "one", "One", is_bot=False)
            storage.upsert_user(2, "two", "Two", is_bot=False)
            storage.upsert_user(3, "robot", "Robot", is_bot=True)

            storage.record_message(
                chat_id=100,
                message_id=1,
                user_id=1,
                username="one",
                first_name="One",
                is_bot=False,
                day=date(2026, 4, 20),
                is_forward_public=False,
                is_video_note=False,
            )
            storage.record_message(
                chat_id=100,
                message_id=2,
                user_id=2,
                username="two",
                first_name="Two",
                is_bot=False,
                day=date(2026, 4, 20),
                is_forward_public=False,
                is_video_note=False,
            )
            candidates = storage.list_rep_candidates(chat_id=100, exclude_user_id=1)
            self.assertEqual([candidate.user_id for candidate in candidates], [2])

    def test_reaction_received_uses_reaction_day(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            storage.record_message(
                chat_id=1,
                message_id=10,
                user_id=5,
                username="author",
                first_name="Author",
                is_bot=False,
                day=date(2026, 4, 20),
                is_forward_public=False,
                is_video_note=False,
            )
            storage.apply_reaction_delta(
                chat_id=1,
                message_id=10,
                day=date(2026, 4, 27),
                delta=1,
            )

            first_week = {item.user_id: item for item in storage.get_week_stats(1, date(2026, 4, 20))}
            second_week = {item.user_id: item for item in storage.get_week_stats(1, date(2026, 4, 27))}
            self.assertEqual(first_week[5].reactions_received, 0)
            self.assertEqual(second_week[5].reactions_received, 1)

    def test_zero_activity_users_are_filtered_out(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            storage.upsert_user(1, "idle", "Idle", is_bot=False)
            self.assertEqual(storage.get_week_stats(1, date(2026, 4, 20)), [])

    def test_multi_chat_stats_and_reports_are_isolated(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            day = date(2026, 4, 20)

            storage.record_message(
                chat_id=100,
                message_id=1,
                user_id=1,
                username="one",
                first_name="One",
                is_bot=False,
                day=day,
                is_forward_public=False,
                is_video_note=False,
            )
            storage.record_message(
                chat_id=200,
                message_id=1,
                user_id=2,
                username="two",
                first_name="Two",
                is_bot=False,
                day=day,
                is_forward_public=False,
                is_video_note=False,
            )

            chat_100 = storage.get_week_stats(100, day)
            chat_200 = storage.get_week_stats(200, day)
            self.assertEqual([item.user_id for item in chat_100], [1])
            self.assertEqual([item.user_id for item in chat_200], [2])

            storage.mark_report_posted(100, day, datetime(2026, 4, 26, 21, 0, tzinfo=ZoneInfo("Europe/Moscow")))
            self.assertTrue(storage.report_already_posted(100, day))
            self.assertFalse(storage.report_already_posted(200, day))

    def test_summary_preview_does_not_save_titles(self) -> None:
        try:
            from bot.config import Settings
            from bot.handlers import BotHandlers
        except ModuleNotFoundError:
            self.skipTest("telegram package is not installed in the local test environment")

        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            week_start = date(2026, 4, 20)
            storage.record_message(
                chat_id=100,
                message_id=1,
                user_id=1,
                username="one",
                first_name="One",
                is_bot=False,
                day=week_start,
                is_forward_public=False,
                is_video_note=False,
            )
            storage.record_message(
                chat_id=100,
                message_id=2,
                user_id=2,
                username="two",
                first_name="Two",
                is_bot=False,
                day=week_start,
                is_forward_public=False,
                is_video_note=False,
            )
            settings = Settings(
                bot_token="x",
                chat_id=None,
                timezone=ZoneInfo("Europe/Moscow"),
                post_hour=21,
                post_minute=0,
                db_path=Path(tmpdir) / "bot.sqlite3",
            )
            handlers = BotHandlers(storage=storage, settings=settings)

            text = handlers._build_summary_payload(100, week_start, save_titles=False)

            self.assertIn("Промежуточные итоги недели", text)
            self.assertEqual(storage.get_titles_for_user(100, week_start, 1), [])

    def test_message_content_and_salute_transcript_storage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "bot.sqlite3")
            storage.init()
            storage.upsert_user(1, "one", "One", is_bot=False)
            storage.save_message_content(
                chat_id=100,
                message_id=1,
                user_id=1,
                message_date=date(2026, 4, 20),
                message_type="voice",
                text_content=None,
                reply_to_message_id=None,
                file_id="voice-file",
                transcript_status="missing",
            )
            saved = storage.save_salute_transcript(
                chat_id=100,
                reply_to_message_id=1,
                transcript_text="Привет, это расшифровка",
            )
            rows = storage.get_recent_message_content(chat_id=100, limit=10)

            self.assertTrue(saved)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["transcript_text"], "Привет, это расшифровка")
            self.assertEqual(rows[0]["transcript_source"], "salute")
            self.assertEqual(rows[0]["transcript_status"], "done")


if __name__ == "__main__":
    unittest.main()
