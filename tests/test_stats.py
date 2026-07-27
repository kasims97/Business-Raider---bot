import unittest

from bot.stats import (
    PlayerTotals,
    compute_current_streaks,
    format_kill_matrix,
    format_table,
    league_rank,
    merge_totals,
    pick_titles,
    sort_by_league_points,
    sort_players,
)


def make_player(user_id, name, played, kills, tops):
    return PlayerTotals(user_id=user_id, username=None, first_name=name, played=played, kills=kills, tops=tops)


class StatsTests(unittest.TestCase):
    def test_deaths_is_played_minus_tops(self) -> None:
        p = make_player(1, "Ильхам", played=20, kills=26, tops=7)
        self.assertEqual(p.deaths, 13)

    def test_kd_matches_screenshot_example(self) -> None:
        p = make_player(1, "Ильхам", played=20, kills=26, tops=7)
        self.assertAlmostEqual(p.kd, 2.0, places=2)

    def test_kd_with_zero_deaths_falls_back_to_kills(self) -> None:
        p = make_player(1, "Идеал", played=5, kills=10, tops=5)
        self.assertEqual(p.deaths, 0)
        self.assertEqual(p.kd, 10.0)

    def test_deaths_never_negative(self) -> None:
        # defensive: tops can't exceed played in practice, but guard anyway
        p = make_player(1, "X", played=3, kills=1, tops=5)
        self.assertEqual(p.deaths, 0)

    def test_sort_players_by_tops_then_kd_then_kills(self) -> None:
        a = make_player(1, "A", played=10, kills=5, tops=3)
        b = make_player(2, "B", played=10, kills=8, tops=3)
        c = make_player(3, "C", played=10, kills=20, tops=5)
        ranked = sort_players([a, b, c])
        self.assertEqual([p.user_id for p in ranked], [3, 2, 1])

    def test_league_points_and_rank_thresholds(self) -> None:
        self.assertEqual(league_rank(0), "🥉 Бронза")
        self.assertEqual(league_rank(149), "🥉 Бронза")
        self.assertEqual(league_rank(150), "🥈 Серебро")
        self.assertEqual(league_rank(350), "🥇 Золото")
        self.assertEqual(league_rank(600), "👑 Легенда")

    def test_sort_by_league_points(self) -> None:
        a = make_player(1, "A", played=10, kills=10, tops=1)  # 1*3 + 10 = 13
        b = make_player(2, "B", played=10, kills=1, tops=5)  # 5*3 + 1 = 16
        ranked = sort_by_league_points([a, b])
        self.assertEqual([p.user_id for p in ranked], [2, 1])

    def test_merge_totals_sums_across_tournaments(self) -> None:
        a1 = make_player(1, "A", played=5, kills=5, tops=1)
        a2 = make_player(1, "A", played=5, kills=3, tops=2)
        merged = merge_totals([a1, a2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].played, 10)
        self.assertEqual(merged[0].kills, 8)
        self.assertEqual(merged[0].tops, 3)

    def test_format_table_percentages_match_screenshot(self) -> None:
        players = [
            make_player(1, "Ильхам", 20, 26, 7),
            make_player(2, "Касим", 20, 11, 6),
            make_player(3, "Кирилл", 20, 16, 4),
            make_player(4, "Ярик", 20, 16, 3),
            make_player(5, "Хан", 20, 6, 0),
        ]
        text = format_table(tournament_name="Test", game_label="PUBG", players=players)
        self.assertIn("35%", text)  # Ильхам: 7/20 tops
        self.assertIn("2.00", text)  # Ильхам KD

    def test_format_table_handles_empty(self) -> None:
        text = format_table(tournament_name="Test", game_label="PUBG", players=[])
        self.assertIn("Пока ни одного", text)

    def test_kill_matrix_counts_pairs_and_ignores_random_kills(self) -> None:
        players = [make_player(1, "A", 5, 3, 1), make_player(2, "B", 5, 2, 0)]
        # random kills (victim None) never appear in kill_pairs since storage filters them out
        pairs = {(1, 2): 3, (2, 1): 1}
        text = format_kill_matrix(players, pairs)
        self.assertIn("Любимая жертва", text)
        self.assertIn("B", text)

    def test_kill_matrix_empty(self) -> None:
        text = format_kill_matrix([make_player(1, "A", 1, 0, 0)], {})
        self.assertIn("рандомные фраги", text)

    def test_titles_champion_and_butcher(self) -> None:
        players = [
            make_player(1, "A", played=10, kills=20, tops=5),
            make_player(2, "B", played=10, kills=2, tops=1),
        ]
        titles = pick_titles(players, {})
        title_names = {t[1] for t in titles}
        self.assertIn("🥇 ЧЕМПИОН", title_names)
        self.assertIn("🔪 МЯСНИК", title_names)
        champion = next(t for t in titles if t[1] == "🥇 ЧЕМПИОН")
        self.assertEqual(champion[0], 1)

    def test_titles_skip_chicken_when_everyone_has_tops(self) -> None:
        players = [
            make_player(1, "A", played=10, kills=5, tops=2),
            make_player(2, "B", played=10, kills=5, tops=1),
        ]
        titles = pick_titles(players, {})
        title_names = {t[1] for t in titles}
        self.assertNotIn("🐔 КУРИЦА", title_names)

    def test_titles_empty_players(self) -> None:
        self.assertEqual(pick_titles([], {}), [])

    def test_streaks_current_win_streak(self) -> None:
        results = [{1: True}, {1: True}, {1: False}, {1: True}, {1: True}, {1: True}]
        streaks = compute_current_streaks(results)
        self.assertEqual(streaks[1], ("🔥", 3))

    def test_streaks_current_loss_streak(self) -> None:
        results = [{1: True}, {1: False}, {1: False}]
        streaks = compute_current_streaks(results)
        self.assertEqual(streaks[1], ("💀", 2))

    def test_streaks_ignore_players_who_did_not_play_that_match(self) -> None:
        results = [{1: True}, {}, {1: True}]
        streaks = compute_current_streaks(results)
        self.assertEqual(streaks[1], ("🔥", 2))

    def test_streaks_below_threshold_not_reported(self) -> None:
        results = [{1: False}, {1: True}]
        streaks = compute_current_streaks(results)
        self.assertNotIn(1, streaks)


if __name__ == "__main__":
    unittest.main()
