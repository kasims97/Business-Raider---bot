import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from telegram import MessageEntity

from bot.config import Settings
from bot.handlers import BotHandlers


def make_settings() -> Settings:
    return Settings(
        bot_token="dummy",
        timezone=ZoneInfo("Europe/Moscow"),
        db_path="/tmp/unused.sqlite3",
    )


def make_user(user_id: int, first_name: str, username: str | None = None, is_bot: bool = False):
    return SimpleNamespace(id=user_id, first_name=first_name, username=username, is_bot=is_bot)


class RegisterIncidentalUsersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MagicMock()
        self.handlers = BotHandlers(storage=self.storage, settings=make_settings())
        self.now = datetime.now(ZoneInfo("Europe/Moscow"))

    def test_registers_reply_to_user(self) -> None:
        replied_user = make_user(2, "Ильхам")
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(from_user=replied_user),
            entities=(),
        )
        self.handlers._register_incidental_users(message, chat_id=100, now=self.now)
        self.storage.register_chat_presence.assert_called_once_with(
            chat_id=100, user_id=2, username=None, first_name="Ильхам", is_bot=False, seen_at=self.now.date()
        )

    def test_registers_text_mention_user(self) -> None:
        mentioned_user = make_user(3, "Касим", username="kasim")
        entity = SimpleNamespace(type=MessageEntity.TEXT_MENTION, user=mentioned_user)
        message = SimpleNamespace(reply_to_message=None, entities=(entity,))
        self.handlers._register_incidental_users(message, chat_id=100, now=self.now)
        self.storage.register_chat_presence.assert_called_once_with(
            chat_id=100, user_id=3, username="kasim", first_name="Касим", is_bot=False, seen_at=self.now.date()
        )

    def test_skips_bots(self) -> None:
        bot_user = make_user(4, "SomeBot", is_bot=True)
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(from_user=bot_user),
            entities=(),
        )
        self.handlers._register_incidental_users(message, chat_id=100, now=self.now)
        self.storage.register_chat_presence.assert_not_called()

    def test_no_reply_no_mentions_registers_nobody(self) -> None:
        message = SimpleNamespace(reply_to_message=None, entities=())
        self.handlers._register_incidental_users(message, chat_id=100, now=self.now)
        self.storage.register_chat_presence.assert_not_called()


class PendingTextGatingTests(unittest.IsolatedAsyncioTestCase):
    """Regression test: tapping '+ Add player' must not swallow chat messages
    that are unrelated and stale — but a plain (non-reply) message sent right
    away is accepted as a fallback in case ForceReply didn't engage on the
    user's client, since a normal reply is the common case (mention makes
    `selective=True` target them) and this fallback only covers the rest."""

    def setUp(self) -> None:
        self.storage = MagicMock()
        self.handlers = BotHandlers(storage=self.storage, settings=make_settings())
        self.chat = SimpleNamespace(id=100)
        self.user = make_user(1, "Касим")

    def _make_update(self, text: str, reply_to_message_id: int | None):
        reply_to = SimpleNamespace(message_id=reply_to_message_id) if reply_to_message_id else None
        bot_stub = SimpleNamespace(delete_message=AsyncMock())
        message = SimpleNamespace(
            text=text,
            date=datetime.now(ZoneInfo("Europe/Moscow")),
            reply_to_message=reply_to,
            reply_text=AsyncMock(),
            get_bot=MagicMock(return_value=bot_stub),
        )
        return SimpleNamespace(effective_message=message, effective_user=self.user, effective_chat=self.chat)

    @staticmethod
    def _pending(prompt_message_id: int, *, age_seconds: float) -> dict:
        created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {
            "kind": "add_player:menu",
            "prompt_message_id": prompt_message_id,
            "created_at": created_at.isoformat(),
        }

    async def test_stale_unrelated_message_is_ignored(self) -> None:
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=200)
        update = self._make_update("протестим короч", reply_to_message_id=None)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_not_called()
        self.storage.clear_pending_input.assert_not_called()

    async def test_fresh_non_reply_message_is_accepted_as_fallback(self) -> None:
        # ForceReply should make this a reply automatically; this covers clients where it doesn't.
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=5)
        self.storage.list_players.return_value = []
        update = self._make_update("Вова", reply_to_message_id=None)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_called_once()

    async def test_stale_reply_to_wrong_message_is_ignored(self) -> None:
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=200)
        update = self._make_update("Вова", reply_to_message_id=999)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_not_called()

    async def test_reply_to_correct_prompt_adds_player(self) -> None:
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=5)
        self.storage.list_players.return_value = []
        update = self._make_update("Вова", reply_to_message_id=42)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_called_once()
        self.storage.clear_pending_input.assert_called_once_with(chat_id=100, user_id=1)

    async def test_comma_separates_multiple_names_space_does_not(self) -> None:
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=5)
        self.storage.list_players.return_value = []
        update = self._make_update("Хан Аднаев", reply_to_message_id=42)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_called_once_with(
            chat_id=100, first_name="Хан Аднаев", seen_at=update.effective_message.date.date()
        )

    async def test_comma_separated_names_added_individually(self) -> None:
        self.storage.get_pending_input.return_value = self._pending(42, age_seconds=5)
        self.storage.list_players.return_value = []
        update = self._make_update("Хан, Вова", reply_to_message_id=42)
        await self.handlers._handle_pending_text(update)
        names = [call.kwargs["first_name"] for call in self.storage.add_guest_player.call_args_list]
        self.assertEqual(names, ["Хан", "Вова"])


class MvpSelfVoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.storage = MagicMock()
        self.handlers = BotHandlers(storage=self.storage, settings=make_settings())

    async def test_cannot_vote_for_self(self) -> None:
        query = SimpleNamespace(
            from_user=make_user(1, "Касим"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_markup=None),
        )
        await self.handlers._handle_mvp_callback(query, ["mv", "5", "1"])
        query.answer.assert_called_once_with("За себя голосовать нельзя.", show_alert=True)
        self.storage.cast_mvp_vote.assert_not_called()

    async def test_can_vote_for_someone_else(self) -> None:
        self.storage.get_mvp_tally.return_value = []
        query = SimpleNamespace(
            from_user=make_user(1, "Касим"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_markup=None),
        )
        await self.handlers._handle_mvp_callback(query, ["mv", "5", "2"])
        self.storage.cast_mvp_vote.assert_called_once_with(tournament_id=5, voter_id=1, pick_user_id=2)


class HistoryViewSoloWinnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MagicMock()
        self.handlers = BotHandlers(storage=self.storage, settings=make_settings())

    def test_history_shows_the_survivor_as_winner_not_the_eliminated(self) -> None:
        # top == 0 means alive/won, top == 1 means eliminated (post elimination-model redesign).
        self.storage.get_active_tournament.return_value = None
        self.storage.list_finished_tournaments.return_value = [
            {"tournament_id": 1, "name": "Test", "team_mode": 0}
        ]
        self.storage.list_recent_matches.return_value = [
            {"match_id": 10, "match_no": 1, "winner_team": None}
        ]
        self.storage.get_match_kills.return_value = []
        self.storage.get_match_players.return_value = [
            {"first_name": "Ильхам", "top": 0},  # survivor / winner
            {"first_name": "Хан", "top": 1},  # eliminated
        ]
        text, _ = self.handlers._render_history_view(chat_id=100)
        self.assertIn("🏆 Ильхам", text)
        self.assertNotIn("🏆 Хан", text)


if __name__ == "__main__":
    unittest.main()
