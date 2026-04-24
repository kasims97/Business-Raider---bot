from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    Defaults,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from bot.config import Settings
from bot.handlers import BotHandlers
from bot.storage import Storage


def build_application() -> Application:
    settings = Settings.from_env()
    settings.ensure_directories()
    settings.configure_logging()

    storage = Storage(settings.db_path)
    storage.init()

    application = (
        Application.builder()
        .token(settings.bot_token)
        .defaults(Defaults(tzinfo=settings.timezone))
        .post_init(post_init)
        .build()
    )

    handlers = BotHandlers(storage=storage, settings=settings)

    application.add_handler(MessageHandler(~filters.StatusUpdate.ALL, handlers.on_message))
    application.add_handler(CallbackQueryHandler(handlers.on_callback_query, pattern=r"^rep:"))
    application.add_handler(
        MessageReactionHandler(
            handlers.on_message_reaction,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED,
        )
    )

    application.job_queue.run_repeating(
        handlers.post_weekly_if_due,
        interval=60,
        first=10,
        name="weekly-summary-check",
    )

    return application


async def post_init(application: Application) -> None:
    group_commands = [
        BotCommand("top", "рейтинг недели"),
        BotCommand("myrating", "твоя статистика"),
        BotCommand("summary", "промежуточные итоги недели"),
        BotCommand("rep", "дать +rep или -rep участнику"),
        BotCommand("about", "что умеет бот"),
    ]
    private_commands = [
        BotCommand("start", "как пользоваться ботом"),
        BotCommand("about", "что умеет бот"),
    ]
    await application.bot.set_my_commands(
        group_commands,
        scope=BotCommandScopeAllGroupChats(),
    )
    await application.bot.set_my_commands(
        private_commands,
        scope=BotCommandScopeAllPrivateChats(),
    )


def main() -> None:
    application = build_application()
    logging.getLogger(__name__).info("Bot started")
    application.run_polling(
        allowed_updates=["message", "message_reaction", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
