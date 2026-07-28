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
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Test Cup")
        self.storage.set_draft_game(tid, "pubg")
        self.storage.set_draft_mode(tid, 0)
        for uid in player_ids:
            self.storage.toggle_draft_player(tid, uid, 0)
        return tid

    def _team_of(self, tournament_id: int, user_id: int) -> int | None:
        row = next((r for r in self.storage.get_draft_players(tournament_id) if r["user_id"] == user_id), None)
        return row["team"] if row is not None else None

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
        tid1 = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="28.07")
        tid2 = self.storage.create_setup_draft(chat_id=CHAT, created_by=2, default_name="28.07")
        self.assertEqual(tid1, tid2)

    def test_create_setup_draft_only_prefills_name(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="28.07")
        draft = self.storage.get_tournament(tid)
        self.assertEqual(draft["name"], "28.07")
        self.assertIsNone(draft["game"])
        self.assertIsNone(draft["team_mode"])

    def test_create_setup_draft_dedupes_name_within_chat(self) -> None:
        tid1 = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="28.07")
        self.storage.set_draft_name(tid1, "28.07")  # simulate a finished/renamed earlier draft with this name
        self.storage.cancel_draft(tid1)
        # fake a finished tournament with the same name to trigger dedup on the next draft
        with self.storage.connect() as conn:
            conn.execute(
                "INSERT INTO tournaments (chat_id, name, game, team_mode, status, created_by, created_at) "
                "VALUES (?, '28.07', 'pubg', 0, 'finished', 1, '2026-01-01T00:00:00+00:00')",
                (CHAT,),
            )
        tid2 = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="28.07")
        draft = self.storage.get_tournament(tid2)
        self.assertEqual(draft["name"], "28.07 (2)")

    def test_live_match_not_counted_in_totals(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        totals = self.storage.get_tournament_totals(tid)
        self.assertEqual(totals, [])  # nothing saved yet

    def test_save_and_advance_produces_totals_and_new_live_match(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=3)
        # player 1 was never eliminated, so they're the automatic survivor/winner
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
        # fresh match starts with everyone alive regardless of the previous match
        for p in self.storage.get_match_players(new_match_id):
            self.assertEqual(p["kills"], 0)
            self.assertEqual(p["top"], 0)

    def test_undo_last_action_removes_most_recent_kill_and_revives_victim(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=3)
        undone = self.storage.undo_last_action(match_id=match_id)
        self.assertIsNotNone(undone)
        rows = {r["user_id"]: r for r in self.storage.get_match_players(match_id)}
        self.assertEqual(rows[1]["kills"], 1)  # only the first kill remains
        self.assertEqual(rows[3]["top"], 0)  # revived — that kill was undone
        self.assertEqual(rows[2]["top"], 1)  # untouched, still eliminated

    def test_undo_last_action_with_nothing_to_undo(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.assertIsNone(self.storage.undo_last_action(match_id=match_id))

    def test_zone_death_eliminates_without_crediting_a_kill(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=0, victim_id=2)
        rows = {r["user_id"]: r for r in self.storage.get_match_players(match_id)}
        self.assertEqual(rows[2]["top"], 1)
        self.assertEqual(rows[2]["kills"], 0)
        self.assertEqual(rows[1]["kills"], 0)  # no one credited

    def test_undo_last_action_reverts_a_zone_death(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=0, victim_id=2)
        undone = self.storage.undo_last_action(match_id=match_id)
        self.assertIsNotNone(undone)
        rows = {r["user_id"]: r for r in self.storage.get_match_players(match_id)}
        self.assertEqual(rows[2]["top"], 0)

    def test_kill_timing_averages_reflect_when_kills_happen(self) -> None:
        tid = self._setup_solo_tournament(player_ids=(1, 2, 3))
        match_id = self.storage.start_tournament(tid)
        # 3 players alive at the start. Player 1 strikes immediately (3 still alive).
        self.storage.record_kill(match_id=match_id, killer_id=1, victim_id=2)
        # now only 2 remain (players 1 and 3) — player 3 waits until this exact moment.
        self.storage.record_kill(match_id=match_id, killer_id=3, victim_id=1)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        averages = self.storage.get_kill_timing_averages(tid)
        self.assertEqual(averages[1], 3.0)
        self.assertNotIn(2, averages)  # player 2 never got a kill
        self.assertEqual(averages[3], 2.0)

    def test_kill_timing_averages_ignore_zone_deaths_as_a_killer(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=0, victim_id=2)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        averages = self.storage.get_kill_timing_averages(tid)
        self.assertNotIn(0, averages)

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
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Team Cup")
        self.storage.set_draft_game(tid, "cs")
        self.storage.set_draft_mode(tid, -1)  # team mode chosen, count not known yet
        self.storage.set_draft_team_step(tid, 1)
        self.storage.toggle_draft_player(tid, 1, 1)  # team 1 screen -> in
        self.storage.toggle_draft_player(tid, 2, 1)  # team 1 screen -> in
        self.storage.advance_draft_team_step(tid)  # move to team 2 screen
        self.storage.toggle_draft_player(tid, 3, 2)  # team 2 screen -> in
        self.storage.set_draft_mode(tid, 2)  # finalized, as handlers._start_tournament does
        match_id = self.storage.start_tournament(tid)
        self.storage.set_winner_team(match_id=match_id, team=1)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        totals = {p.user_id: p for p in self.storage.get_tournament_totals(tid)}
        self.assertEqual(totals[1].tops, 1)
        self.assertEqual(totals[2].tops, 1)
        self.assertEqual(totals[3].tops, 0)

    def test_toggle_draft_player_toggles_within_current_team_step(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Cup")
        self.storage.set_draft_mode(tid, -1)
        self.storage.set_draft_team_step(tid, 1)
        self.storage.toggle_draft_player(tid, 1, 1)  # in team 1
        self.assertEqual(self._team_of(tid, 1), 1)
        self.storage.toggle_draft_player(tid, 1, 1)  # tap again -> out
        self.assertIsNone(self._team_of(tid, 1))

    def test_toggle_draft_player_does_not_touch_player_locked_in_earlier_team(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Cup")
        self.storage.set_draft_mode(tid, -1)
        self.storage.set_draft_team_step(tid, 1)
        self.storage.toggle_draft_player(tid, 1, 1)  # locked into team 1
        self.storage.advance_draft_team_step(tid)  # now on team 2 screen
        # tapping player 1 while looking at the team-2 screen must not move them
        self.storage.toggle_draft_player(tid, 1, 2)
        self.assertEqual(self._team_of(tid, 1), 1)

    def test_advance_draft_team_step_caps_at_max_teams(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Cup")
        self.storage.set_draft_mode(tid, -1)
        self.storage.set_draft_team_step(tid, 1)
        for _ in range(10):
            self.storage.advance_draft_team_step(tid)
        self.assertEqual(self.storage.get_tournament(tid)["team_step"], 4)

    def test_four_team_tournament_totals(self) -> None:
        self.storage.register_chat_presence(
            chat_id=CHAT, user_id=4, username=None, first_name="Ярик", is_bot=False, seen_at=date(2026, 1, 1)
        )
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Battle Royale Teams")
        self.storage.set_draft_game(tid, "pubg")
        self.storage.set_draft_mode(tid, -1)
        self.storage.set_draft_team_step(tid, 1)
        for uid in (1, 2, 3, 4):
            self.storage.toggle_draft_player(tid, uid, uid)  # each on their own team's screen
            if uid < 4:
                self.storage.advance_draft_team_step(tid)
        self.storage.set_draft_mode(tid, 4)
        match_id = self.storage.start_tournament(tid)
        self.storage.set_winner_team(match_id=match_id, team=3)
        self.storage.save_match_and_advance(tournament_id=tid, match_id=match_id)
        totals = {p.user_id: p for p in self.storage.get_tournament_totals(tid)}
        self.assertEqual(totals[3].tops, 1)
        self.assertEqual(totals[1].tops, 0)
        self.assertEqual(totals[2].tops, 0)
        self.assertEqual(totals[4].tops, 0)

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

    def test_match_has_progress_true_after_zone_death(self) -> None:
        tid = self._setup_solo_tournament()
        match_id = self.storage.start_tournament(tid)
        self.storage.record_kill(match_id=match_id, killer_id=0, victim_id=1)
        self.assertTrue(self.storage.match_has_progress(match_id))

    def test_match_has_progress_true_after_team_win_set(self) -> None:
        tid = self.storage.create_setup_draft(chat_id=CHAT, created_by=1, default_name="Team Cup")
        self.storage.set_draft_game(tid, "cs")
        self.storage.set_draft_mode(tid, -1)
        self.storage.set_draft_team_step(tid, 1)
        self.storage.toggle_draft_player(tid, 1, 1)  # team 1
        self.storage.advance_draft_team_step(tid)
        self.storage.toggle_draft_player(tid, 2, 2)  # team 2
        self.storage.set_draft_mode(tid, 2)
        match_id = self.storage.start_tournament(tid)
        self.assertFalse(self.storage.match_has_progress(match_id))
        self.storage.set_winner_team(match_id=match_id, team=1)
        self.assertTrue(self.storage.match_has_progress(match_id))

    def test_match_results_sequence_reflects_top_and_team_wins(self) -> None:
        tid = self._setup_solo_tournament()
        match1 = self.storage.start_tournament(tid)
        # player 1 survives (never eliminated), player 2 is killed off
        self.storage.record_kill(match_id=match1, killer_id=1, victim_id=2)
        match2 = self.storage.save_match_and_advance(tournament_id=tid, match_id=match1)
        self.storage.record_kill(match_id=match2, killer_id=1, victim_id=2)
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
