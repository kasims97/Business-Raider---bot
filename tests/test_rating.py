import unittest
from datetime import date, datetime
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


if __name__ == "__main__":
    unittest.main()
