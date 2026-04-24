from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime

from telegram import MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.rating import (
    format_personal_stats,
    format_ranking,
    format_weekly_summary,
    pick_titles,
    sort_for_ranking,
    week_start_for_dt,
)
from bot.storage import Storage

logger = logging.getLogger(__name__)

COMMAND_TOP = re.compile(r"^/(топ|top)(?:@\w+)?$")
COMMAND_MY = re.compile(r"^/(мойрейтинг|myrating)(?:@\w+)?$")
COMMAND_SUMMARY = re.compile(r"^/(итоги|summary)(?:@\w+)?$")
COMMAND_ABOUT = re.compile(r"^/about(?:@\w+)?$")
COMMAND_REP = re.compile(r"^/([+-])rep(?:@\w+)?$")


class BotHandlers:
    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        chat = update.effective_chat
        if chat is None:
            return

        if chat.type == ChatType.PRIVATE:
            await self._handle_private_message(update)
            return

        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return

        if not self._is_target_chat(chat.id):
            return

        now = self._localized(message.date)
        text = (message.text or "").strip()

        if COMMAND_TOP.match(text):
            await self._send_top(update)
            return
        if COMMAND_MY.match(text):
            await self._send_personal(update)
            return
        if COMMAND_SUMMARY.match(text):
            await self._send_summary(update)
            return
        if COMMAND_ABOUT.match(text):
            await self._send_about(update)
            return
        rep_match = COMMAND_REP.match(text)
        if rep_match:
            await self._handle_rep(update, rep_match.group(1))
            return

        self.storage.record_message(
            chat_id=message.chat_id,
            message_id=message.message_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            day=now.date(),
            is_forward_public=self._is_public_forward(message),
            is_video_note=message.video_note is not None,
        )

        mentioned_ids = self._extract_mentions(message)
        self.storage.increment_mentions(mentioned_ids, now.date())

    async def on_message_reaction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reaction = update.message_reaction
        if reaction is None:
            return
        if not self._is_target_chat(reaction.chat.id):
            return

        current_day = self._localized(reaction.date).date()
        delta = len(reaction.new_reaction) - len(reaction.old_reaction)
        if delta == 0:
            return

        applied = self.storage.apply_reaction_delta(
            chat_id=reaction.chat.id,
            message_id=reaction.message_id,
            delta=delta,
        )
        if not applied:
            logger.info(
                "Skipping reaction for unknown message %s/%s",
                reaction.chat.id,
                reaction.message_id,
            )
            return

        if reaction.user is not None:
            self.storage.increment_reactions_given(
                user_id=reaction.user.id,
                username=reaction.user.username,
                first_name=reaction.user.first_name,
                day=current_day,
                delta=delta,
            )

    async def post_weekly_if_due(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(self.settings.timezone)
        if now.weekday() != 6:
            return
        if (now.hour, now.minute) < (self.settings.post_hour, self.settings.post_minute):
            return

        week_start = week_start_for_dt(now)
        if self.storage.report_already_posted(week_start):
            return

        chat_id = self._active_chat_id()
        if chat_id is None:
            return

        text = self._build_summary_payload(week_start)
        await context.bot.send_message(chat_id=chat_id, text=text)
        self.storage.mark_report_posted(week_start, now)

    async def _send_top(self, update: Update) -> None:
        now = datetime.now(self.settings.timezone)
        week_start = week_start_for_dt(now)
        ranked = sort_for_ranking(self.storage.get_week_stats(week_start))
        text = format_ranking(ranked, "Текущий рейтинг недели")
        await update.effective_message.reply_text(text, do_quote=False)

    async def _send_personal(self, update: Update) -> None:
        user = update.effective_user
        if user is None or update.effective_message is None:
            return

        now = datetime.now(self.settings.timezone)
        week_start = week_start_for_dt(now)
        ranked = sort_for_ranking(self.storage.get_week_stats(week_start))
        for idx, item in enumerate(ranked, start=1):
            if item.user_id == user.id:
                titles = self.storage.get_titles_for_user(week_start, user.id)
                text = format_personal_stats(item, idx, len(ranked), titles)
                await update.effective_message.reply_text(text, do_quote=False)
                return

        await update.effective_message.reply_text(
            "За эту неделю у тебя пока нет активности.",
            do_quote=False,
        )

    async def _send_summary(self, update: Update) -> None:
        if update.effective_message is None:
            return
        now = datetime.now(self.settings.timezone)
        week_start = week_start_for_dt(now)
        text = self._build_summary_payload(week_start)
        await update.effective_message.reply_text(text, do_quote=False)

    async def _send_about(self, update: Update) -> None:
        if update.effective_message is None:
            return
        await update.effective_message.reply_text(
            "Я бот рейтинга чата. Молча считаю активность за неделю: сообщения, "
            "полученные и отданные реакции, упоминания, пересылки из пабликов и кружочки. "
            "По командам показываю рейтинг, а по воскресеньям автоматически выкатываю итоги и титулы недели. "
            "Ещё умею /+rep и /-rep в ответ на сообщение участника: один голос от человека к человеку на неделю, "
            "с возможностью поменять знак.",
            do_quote=False,
        )

    async def _handle_rep(self, update: Update, sign: str) -> None:
        message = update.effective_message
        from_user = update.effective_user
        chat = update.effective_chat
        if message is None or from_user is None or chat is None:
            return

        replied = message.reply_to_message
        target_user = replied.from_user if replied is not None else None
        if replied is None or target_user is None or target_user.is_bot:
            await message.reply_text(
                "Используй эту команду ответом на сообщение участника чата.",
                do_quote=False,
            )
            return

        if target_user.id == from_user.id:
            await message.reply_text(
                "Самому себе rep крутить нельзя.",
                do_quote=False,
            )
            return

        now = self._localized(message.date)
        result = self.storage.apply_rep_vote(
            chat_id=chat.id,
            week_start=week_start_for_dt(now),
            from_user_id=from_user.id,
            from_username=from_user.username,
            from_first_name=from_user.first_name,
            to_user_id=target_user.id,
            to_username=target_user.username,
            to_first_name=target_user.first_name,
            value=1 if sign == "+" else -1,
            voted_at=now,
        )

        target_name = f"@{target_user.username}" if target_user.username else target_user.first_name
        if result == "unchanged":
            text = f"У {target_name} уже стоит {sign}rep от тебя на этой неделе."
        elif result == "flipped":
            text = f"Ок, для {target_name} голос на этой неделе переключён на {sign}rep."
        else:
            text = f"Засчитано: {target_name} получил {sign}rep."
        await message.reply_text(text, do_quote=False)

    def _build_summary_payload(self, week_start):
        stats = self.storage.get_week_stats(week_start)
        ranked = sort_for_ranking(stats)
        title_pairs = pick_titles(stats)
        self.storage.save_titles(week_start, title_pairs)
        by_user_id = {item.user_id: item for item in stats}
        titled_items = [
            (by_user_id[user_id], title)
            for user_id, title in title_pairs
            if user_id in by_user_id
        ]
        return format_weekly_summary(week_start, ranked, titled_items)

    async def _handle_private_message(self, update: Update) -> None:
        if update.effective_message is None:
            return
        text = (update.effective_message.text or "").strip()
        if text.startswith("/start"):
            await update.effective_message.reply_text(
                "Добавь меня в групповой чат, и я сам начну считать активность. "
                "После привязки используй команды /top, /myrating, /summary, /about, а ещё /+rep и /-rep в ответ на сообщение участника.",
                do_quote=False,
            )

    def _active_chat_id(self) -> int | None:
        return self.settings.chat_id or self.storage.get_active_chat_id()

    def _is_target_chat(self, chat_id: int) -> bool:
        active_chat_id = self._active_chat_id()
        if active_chat_id is None:
            self.storage.set_active_chat_id(chat_id)
            logger.info("Bound bot to chat_id=%s", chat_id)
            return True
        return chat_id == active_chat_id

    def _localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.settings.timezone)

    def _extract_mentions(self, message) -> list[int]:
        result: list[int] = []
        usernames: list[str] = []

        for entity in tuple(message.entities or ()) + tuple(message.caption_entities or ()):
            if entity.type == MessageEntity.TEXT_MENTION and entity.user is not None:
                self.storage.upsert_user(
                    entity.user.id,
                    entity.user.username,
                    entity.user.first_name,
                )
                result.append(entity.user.id)

        if message.text:
            usernames.extend(
                mention.lstrip("@").lower()
                for mention in message.parse_entities([MessageEntity.MENTION]).values()
            )
        if message.caption:
            usernames.extend(
                mention.lstrip("@").lower()
                for mention in message.parse_caption_entities([MessageEntity.MENTION]).values()
            )

        if usernames:
            mapping = self.storage.find_user_ids_by_usernames(usernames)
            result.extend(mapping[name] for name in usernames if name in mapping)

        return result

    def _is_public_forward(self, message) -> bool:
        origin = getattr(message, "forward_origin", None)
        if origin is None:
            return False

        origin_type = type(origin).__name__
        if origin_type in {"MessageOriginChannel"}:
            return True

        chat = getattr(origin, "chat", None)
        if chat is not None and getattr(chat, "type", None) == ChatType.CHANNEL:
            return True

        sender_chat = getattr(message, "forward_from_chat", None)
        if sender_chat is not None and sender_chat.type == ChatType.CHANNEL:
            return True

        return False
