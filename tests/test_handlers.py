import unittest
from datetime import datetime
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
    """Regression test: tapping '+ Add player' must not swallow the next
    unrelated chat message as a player name — only an actual reply to the
    bot's own prompt counts."""

    def setUp(self) -> None:
        self.storage = MagicMock()
        self.handlers = BotHandlers(storage=self.storage, settings=make_settings())
        self.chat = SimpleNamespace(id=100)
        self.user = make_user(1, "Касим")

    def _make_update(self, text: str, reply_to_message_id: int | None):
        reply_to = SimpleNamespace(message_id=reply_to_message_id) if reply_to_message_id else None
        message = SimpleNamespace(
            text=text,
            date=datetime.now(ZoneInfo("Europe/Moscow")),
            reply_to_message=reply_to,
            reply_text=AsyncMock(),
        )
        return SimpleNamespace(effective_message=message, effective_user=self.user, effective_chat=self.chat)

    async def test_unrelated_message_is_ignored_not_added_as_player(self) -> None:
        self.storage.get_pending_input.return_value = {"kind": "add_player:menu", "prompt_message_id": 42}
        update = self._make_update("протестим короч", reply_to_message_id=None)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_not_called()
        self.storage.clear_pending_input.assert_not_called()

    async def test_reply_to_wrong_message_is_ignored(self) -> None:
        self.storage.get_pending_input.return_value = {"kind": "add_player:menu", "prompt_message_id": 42}
        update = self._make_update("Вова", reply_to_message_id=999)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_not_called()

    async def test_reply_to_correct_prompt_adds_player(self) -> None:
        self.storage.get_pending_input.return_value = {"kind": "add_player:menu", "prompt_message_id": 42}
        self.storage.list_players.return_value = []
        update = self._make_update("Вова", reply_to_message_id=42)
        await self.handlers._handle_pending_text(update)
        self.storage.add_guest_player.assert_called_once()
        self.storage.clear_pending_input.assert_called_once_with(chat_id=100, user_id=1)


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


if __name__ == "__main__":
    unittest.main()
