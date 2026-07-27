import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
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


if __name__ == "__main__":
    unittest.main()
