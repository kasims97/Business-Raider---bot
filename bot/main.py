from __future__ import annotations

import logging

from telegram.ext import Application, Defaults, MessageHandler, MessageReactionHandler, filters

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
        .build()
    )

    handlers = BotHandlers(storage=storage, settings=settings)

    application.add_handler(MessageHandler(~filters.StatusUpdate.ALL, handlers.on_message))
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


def main() -> None:
    application = build_application()
    logging.getLogger(__name__).info("Bot started")
    application.run_polling(
        allowed_updates=["message", "message_reaction"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
