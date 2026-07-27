import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.storage import Storage

CHAT = 100


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "test.sqlite3")
        self.storage.init()
        # register three chat members
        for uid, name in ((1, "Ильхам"), (2, "Касим"), (3, "Кирилл")):
            self.storage.register_chat_presence(
                chat_id=CHAT, user_id=uid, username=None, first_name=name, is_bot=False, seen_at=date(2026, 1, 1)
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _setup_solo_tournament(self, player_ids=(1, 2, 3)):
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1)
        self.storage.set_draft_name(tid, "Test Cup")
        self.storage.set_draft_game(tid, "pubg")
        self.storage.set_draft_mode(tid, False)
        for uid in player_ids:
            self.storage.toggle_draft_player(tid, uid, False)
        return tid

    def test_guest_players_get_negative_ids_and_dont_collide(self) -> None:
        first = self.storage.add_guest_player(chat_id=CHAT, first_name="Вова", seen_at=date(2026, 1, 1))
        second = self.storage.add_guest_player(chat_id=CHAT, first_name="Даня", seen_at=date(2026, 1, 1))
        self.assertLess(first, 0)
        self.assertLess(second, first)

    def test_list_players_excludes_bots_and_removed(self) -> None:
        players = self.storage.list_players(CHAT)
        self.assertEqual({p.user_id for p in players}, {1, 2, 3})
        self.storage.remove_player(chat_id=CHAT, user_id=2)
        players = self.storage.list_players(CHAT)
        self.assertEqual({p.user_id for p in players}, {1, 3})

    def test_only_one_setup_draft_per_chat(self) -> None:
        tid1 = self.storage.create_setup_draft(chat_id=CHAT, created_by=1)
        tid2 = self.storage.create_setup_draft(chat_id=CHAT, created_by=2)
        self.assertEqual(tid1, tid2)

    def test_live_match_not_counted_in_totals(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.toggle_top(match_id=match_id, user_id=1)
        totals = self.storage.get_tournament_totals(tid)
        self.assertEqual(totals, [])  # nothing saved yet

    def test_save_and_advance_produces_totals_and_new_live_match(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=3)
        self.storage.toggle_top(match_id=match_id, user_id=1)
        new_match_id = self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        self.assertNotEqual(new_match_id, match_id)

        totals = {p.user_id: p for p in self.storage.get_tournament_totals(tid)}
        self.assertEqual(totals[1].kills, 2)
        self.assertEqual(totals[1].tops, 1)
        self.assertEqual(totals[1].played, 1)
        self.assertEqual(totals[1].deaths, 0)
        self.assertEqual(totals[2].tops, 0)
        self.assertEqual(totals[2].deaths, 1)

        # roster carried over into the new live match
        new_players = {p["user_id"] for p in self.storage.get_match_players(new_match_id)}
        self.assertEqual(new_players, {1, 2, 3})
        # fresh match starts with zero kills/tops regardless of previous match
        for p in self.storage.get_match_players(new_match_id):
            self.assertEqual(p["kills"], 0)
            self.assertEqual(p["top"], 0)

    def test_undo_kill_removes_most_recent_only(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=3)
        ok = self.storage.undo_last_kill(match_id=match_id, killer_id=1)
        self.assertTrue(ok)
        rows = self.storage.get_match_players(match_id)
        killer_row = next(r for r in rows if r["user_id"] == 1)
        self.assertEqual(killer_row["kills"], 1)

    def test_undo_kill_with_nothing_to_undo(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        ok = self.storage.undo_last_kill(match_id=match_id, killer_id=1)
        self.assertFalse(ok)

    def test_random_kill_has_no_victim_and_is_excluded_from_matrix(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=None)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        matrix = self.storage.get_kill_matrix(tid)
        self.assertEqual(matrix, {(1, 2): 1})
        totals = {p.user_id: p for p in self.storage.get_tournament_totals(tid)}
        self.assertEqual(totals[1].kills, 2)  # both kills still count toward the killer's total

    def test_team_mode_win_credited_to_whole_team(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1)
        self.storage.set_draft_name(tid, "Team Cup")
        self.storage.set_draft_game(tid, "cs")
        self.storage.set_draft_mode(tid, True)
        self.storage.toggle_draft_player(tid, 1, True)  # -> team A
        self.storage.toggle_draft_player(tid, 2, True)  # -> team A
        self.storage.toggle_draft_player(tid, 3, True)  # -> team A
        self.storage.toggle_draft_player(tid, 3, True)  # -> team B
        match_id = self.storage.start_tournament(tid)
        self.storage.set_winner_team(match_id=match_id, team=1)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        totals = {p.user_id: p for p in self.storage.get_tournament_totals(tid)}
        self.assertEqual(totals[1].tops, 1)
        self.assertEqual(totals[2].tops, 1)
        self.assertEqual(totals[3].tops, 0)

    def test_undo_last_saved_match_removes_kills_and_players(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        self.assertTrue(self.storage.get_tournament_totals(tid))
        ok = self.storage.undo_last_saved_match(tid)
        self.assertTrue(ok)
        self.assertEqual(self.storage.get_tournament_totals(tid), [])

    def test_undo_last_saved_match_with_nothing_saved(self) -> None:
        tid = self._setup_solo_tournament()
        self.storage.start_tournament(tid)
        self.assertFalse(self.storage.undo_last_saved_match(tid))

    def test_finish_tournament_discards_live_match(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.finish_tournament(tid)
        tournament = self.storage.get_tournament(tid)
        self.assertEqual(tournament["status"], "finished")
        self.assertIsNone(self.storage.get_active_tournament(CHAT))
        self.assertIsNone(self.storage.get_live_match(tid))

    def test_different_tournaments_do_not_mix_totals(self) -> None:
        tid1 = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid1)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.save_match_and_advance(tournament_id=tid1, match_id=match_id)
        self.storage.finish_tournament(tid1)

        tid2 = self._setup_solo_tournament()
        match_id2 = self.storage.start_tournament(tid2)
        self.storage.record_kill(match_id=match_id2, killer_id=2, victim_id=1)
        self.storage.save_match_and_advance(tournament_id=tid2, match_id=match_id2)

        totals1 = {p.user_id: p for p in self.storage.get_tournament_totals(tid1)}
        totals2 = {p.user_id: p for p in self.storage.get_tournament_totals(tid2)}
        self.assertEqual(totals1[1].kills, 1)
        self.assertEqual(totals1[2].kills, 0)
        self.assertEqual(totals2[2].kills, 1)
        self.assertEqual(totals2[1].kills, 0)

    def test_league_totals_only_include_finished_tournaments(self) -> None:
        tid1 = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid1)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.save_match_and_advance(tournament_id=tid1, match_id=match_id)
        # not finished yet -> league should be empty
        self.assertEqual(self.storage.get_league_totals(CHAT), [])
        self.storage.finish_tournament(tid1)
        league = {p.user_id: p for p in self.storage.get_league_totals(CHAT)}
        self.assertEqual(league[1].kills, 1)

    def test_chats_are_isolated(self) -> None:
        other_chat = 999
        self.storage.register_chat_presence(
            chat_id=other_chat, user_id=1, username=None, first_name="Ильхам", is_bot=False, seen_at=date(2026, 1, 1)
        )
        players_chat1 = self.storage.list_players(CHAT)
        players_chat2 = self.storage.list_players(other_chat)
        self.assertEqual({p.user_id for p in players_chat1}, {1, 2, 3})
        self.assertEqual({p.user_id for p in players_chat2}, {1})

    def test_pending_input_roundtrip_and_clear(self) -> None:
        self.storage.set_pending_input(chat_id=CHAT, user_id=1, kind="tn_name", prompt_message_id=555)
        pending = self.storage.get_pending_input(chat_id=CHAT, user_id=1)
        self.assertEqual(pending["kind"], "tn_name")
        self.assertEqual(pending["prompt_message_id"], 555)
        # still there until explicitly cleared (caller checks reply-to before clearing)
        self.assertIsNotNone(self.storage.get_pending_input(chat_id=CHAT, user_id=1))
        self.storage.clear_pending_input(chat_id=CHAT, user_id=1)
        self.assertIsNone(self.storage.get_pending_input(chat_id=CHAT, user_id=1))

    def test_settings_default_enabled_and_toggle(self) -> None:
        self.assertTrue(self.storage.get_setting(CHAT, "quips"))
        self.storage.toggle_setting(CHAT, "quips")
        self.assertFalse(self.storage.get_setting(CHAT, "quips"))
        self.storage.toggle_setting(CHAT, "quips")
        self.assertTrue(self.storage.get_setting(CHAT, "quips"))

    def test_match_has_progress_false_for_fresh_match(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.assertFalse(self.storage.match_has_progress(match_id))

    def test_match_has_progress_true_after_kill(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.assertTrue(self.storage.match_has_progress(match_id))

    def test_match_has_progress_true_after_top(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.toggle_top(match_id=match_id, user_id=1)
        self.assertTrue(self.storage.match_has_progress(match_id))

    def test_match_has_progress_true_after_team_win_set(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1)
        self.storage.set_draft_name(tid, "Team Cup")
        self.storage.set_draft_game(tid, "cs")
        self.storage.set_draft_mode(tid, True)
        self.storage.toggle_draft_player(tid, 1, True)
        self.storage.toggle_draft_player(tid, 2, True)
        match_id = self.storage.start_tournament(tid)
        self.assertFalse(self.storage.match_has_progress(match_id))
        self.storage.set_winner_team(match_id=match_id, team=1)
        self.assertTrue(self.storage.match_has_progress(match_id))

    def test_match_results_sequence_reflects_top_and_team_wins(self) -> None:
        tid = self._setup_solo_tournament()
        match1 = self.storage.start_tournament(tid)
        self.storage.toggle_top(match_id=match1, user_id=1)
        match2 = self.storage.save_match_and_advance(tournament_id=tid, match_id=match1)
        self.storage.toggle_top(match_id=match2, user_id=1)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match2)

        sequence = self.storage.get_match_results_sequence(tid)
        self.assertEqual(len(sequence), 2)
        self.assertTrue(sequence[0][1])
        self.assertTrue(sequence[1][1])
        self.assertFalse(sequence[0][2])

    def test_prediction_locks_after_first_saved_match(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.assertFalse(self.storage.has_any_saved_match(tid))
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        self.assertTrue(self.storage.has_any_saved_match(tid))


if __name__ == "__main__":
    unittest.main()
