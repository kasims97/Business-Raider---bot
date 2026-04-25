from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from bot.ai import SummaryError, summarize_chat
from bot.config import Settings
from bot.rating import (
    format_personal_stats,
    format_weekly_summary,
    pick_titles,
    sort_for_ranking,
    week_start_for_dt,
)
from bot.storage import Storage

logger = logging.getLogger(__name__)

COMMAND_MY = re.compile(r"^/(мойрейтинг|myrating)(?:@\w+)?$")
COMMAND_SUMMARY = re.compile(r"^/(итоги|summary)(?:@\w+)?$")
COMMAND_ABOUT = re.compile(r"^/about(?:@\w+)?$")
COMMAND_REP = re.compile(r"^/rep(?:@\w+)?$")
COMMAND_CATCHUP = re.compile(r"^/catchup(?:@\w+)?$")
COMMAND_OLD_REP = re.compile(r"^/([+-])rep(?:@\w+)?$")


class BotHandlers:
    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return

        if chat.type == ChatType.PRIVATE:
            await self._handle_private_message(update)
            return

        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return

        now = self._localized(message.date)
        self.storage.register_chat_presence(
            chat_id=chat.id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            is_bot=user.is_bot,
            seen_at=now.date(),
        )
        text = (message.text or "").strip()

        if self._maybe_capture_salute_transcript(update):
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
        if COMMAND_CATCHUP.match(text):
            await self._send_catchup(update)
            return
        if COMMAND_REP.match(text):
            await self._show_rep_user_picker(update)
            return

        old_rep_match = COMMAND_OLD_REP.match(text)
        if old_rep_match:
            await self._handle_reply_rep(update, old_rep_match.group(1))
            return

        self.storage.record_message(
            chat_id=message.chat_id,
            message_id=message.message_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            is_bot=user.is_bot,
            day=now.date(),
            is_forward_public=self._is_public_forward(message),
            is_video_note=message.video_note is not None,
        )
        self.storage.save_message_content(
            chat_id=chat.id,
            message_id=message.message_id,
            user_id=user.id,
            message_date=now.date(),
            message_type=self._message_type(message),
            text_content=self._message_text_content(message),
            reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
            file_id=self._message_file_id(message),
            transcript_status=self._transcript_status_for_message(message),
        )

        mentioned_ids = self._extract_mentions(message)
        self.storage.increment_mentions(
            chat_id=chat.id,
            user_ids=mentioned_ids,
            day=now.date(),
        )

    async def on_message_reaction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reaction = update.message_reaction
        if reaction is None:
            return

        current_day = self._localized(reaction.date).date()
        delta = len(reaction.new_reaction) - len(reaction.old_reaction)
        logger.info(
            "Reaction update chat_id=%s message_id=%s delta=%s has_user=%s",
            reaction.chat.id,
            reaction.message_id,
            delta,
            reaction.user is not None,
        )
        if delta == 0:
            return

        applied = self.storage.apply_reaction_delta(
            chat_id=reaction.chat.id,
            message_id=reaction.message_id,
            day=current_day,
            delta=delta,
        )
        if not applied:
            logger.info(
                "Skipping reaction for unknown message chat_id=%s message_id=%s. "
                "Бот увидел реакцию, но не видел исходное сообщение.",
                reaction.chat.id,
                reaction.message_id,
            )
            return

        if reaction.user is not None:
            self.storage.increment_reactions_given(
                user_id=reaction.user.id,
                username=reaction.user.username,
                first_name=reaction.user.first_name,
                is_bot=reaction.user.is_bot,
                chat_id=reaction.chat.id,
                day=current_day,
                delta=delta,
            )

    async def on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return

        data = query.data
        if not data.startswith("rep:"):
            return

        if data.startswith("rep:c:"):
            owner_user_id = int(data.split(":")[2])
            if not self._is_callback_owner(update, owner_user_id):
                await query.answer("Это меню не для тебя", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text("Выбор репутации отменён.")
            return

        if data.startswith("rep:u:"):
            _, _, target_user_id, owner_user_id = data.split(":")
            if not self._is_callback_owner(update, int(owner_user_id)):
                await query.answer("Это меню не для тебя", show_alert=True)
                return
            await query.answer()
            await self._show_rep_sign_picker(query, int(target_user_id), int(owner_user_id))
            return

        if data.startswith("rep:v:"):
            _, _, target_user_id, owner_user_id, sign = data.split(":")
            if not self._is_callback_owner(update, int(owner_user_id)):
                await query.answer("Это меню не для тебя", show_alert=True)
                return
            await query.answer()
            await self._apply_rep_from_callback(query, int(target_user_id), sign)

    async def post_weekly_if_due(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(self.settings.timezone)
        if now.weekday() != 6:
            return
        if (now.hour, now.minute) < (self.settings.post_hour, self.settings.post_minute):
            return

        week_start = week_start_for_dt(now)
        for chat_id in self.storage.list_known_chats():
            if self.storage.report_already_posted(chat_id, week_start):
                continue
            if not self.storage.get_week_stats(chat_id, week_start):
                continue
            text = self._build_summary_payload(chat_id, week_start, save_titles=True)
            await context.bot.send_message(chat_id=chat_id, text=text)
            self.storage.mark_report_posted(chat_id, week_start, now)

    async def _send_personal(self, update: Update) -> None:
        user = update.effective_user
        message = update.effective_message
        chat = update.effective_chat
        if user is None or message is None or chat is None:
            return

        now = datetime.now(self.settings.timezone)
        week_start = week_start_for_dt(now)
        ranked = sort_for_ranking(self.storage.get_week_stats(chat.id, week_start))
        for idx, item in enumerate(ranked, start=1):
            if item.user_id == user.id:
                titles = self.storage.get_titles_for_user(chat.id, week_start, user.id)
                text = format_personal_stats(item, idx, len(ranked), titles)
                await message.reply_text(text, do_quote=False)
                return

        await message.reply_text(
            "Пока пусто: на этой неделе у тебя ещё нет активности в рейтинге.",
            do_quote=False,
        )

    async def _send_summary(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        now = datetime.now(self.settings.timezone)
        week_start = week_start_for_dt(now)
        text = self._build_summary_payload(chat.id, week_start, save_titles=False)
        await message.reply_text(text, do_quote=False)

    async def _send_about(self, update: Update) -> None:
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            "Я бот рейтинга чата. Слежу за активностью в течение недели, показываю, кто тащит движ, "
            "а по воскресеньям публикую итоги и титулы. Через /rep или reply-команды /+rep и /-rep можно "
            "дать участнику плюс или минус в репутацию. А через /catchup можно получить краткую выжимку "
            "последних 100 сообщений.",
            do_quote=False,
        )

    async def _send_catchup(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return

        rows = self.storage.get_recent_message_content(chat_id=chat.id, limit=100)
        if not rows:
            await message.reply_text(
                "Пока нечего пересказывать. Бот ещё не накопил сообщения для выжимки.",
                do_quote=False,
            )
            return

        status_message = await message.reply_text(
            "Собираю краткую выжимку последних 100 сообщений...",
            do_quote=False,
        )

        blocks: list[str] = []
        missing_audio = 0
        for row in rows:
            if row["is_bot"] and row["username"] and row["username"].lower() == self.settings.salute_bot_username:
                continue
            block = self._message_row_to_summary_block(row)
            if block is None:
                if row["message_type"] in {"voice", "video_note"} and row["transcript_status"] != "done":
                    missing_audio += 1
                continue
            blocks.append(block)

        if not blocks:
            await status_message.edit_text(
                "Пока нечего пересказывать: в последних сообщениях нет доступного текста для резюме."
            )
            return

        try:
            summary = summarize_chat(
                settings=self.settings,
                transcript_blocks=blocks,
                missing_audio_count=missing_audio,
            )
        except SummaryError:
            await status_message.edit_text(
                "Не получилось собрать резюме прямо сейчас. Попробуй ещё раз чуть позже."
            )
            return

        if missing_audio:
            summary = (
                f"{summary}\n\nНе всё аудио вошло в резюме: часть голосовых и кружочков ещё без расшифровки."
            )
        await status_message.edit_text(summary)

    async def _show_rep_user_picker(self, update: Update) -> None:
        message = update.effective_message
        from_user = update.effective_user
        chat = update.effective_chat
        if message is None or from_user is None or chat is None:
            return

        candidates = self.storage.list_rep_candidates(
            chat_id=chat.id,
            exclude_user_id=from_user.id,
        )
        if not candidates:
            await message.reply_text(
                "Пока некого выбирать. Бот ещё не успел познакомиться с другими участниками чата.",
                do_quote=False,
            )
            return

        labels = self._build_candidate_labels(candidates)
        keyboard = []
        for candidate in candidates:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        labels[candidate.user_id],
                        callback_data=self._rep_pick_user_data(
                            target_user_id=candidate.user_id,
                            owner_user_id=from_user.id,
                        ),
                    )
                ]
            )
        keyboard.append(
            [InlineKeyboardButton("Отмена", callback_data=self._rep_cancel_data(from_user.id))]
        )
        await message.reply_text(
            "Кому изменить репутацию?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            do_quote=False,
        )

    async def _show_rep_sign_picker(self, query, target_user_id: int, owner_user_id: int) -> None:
        if query.message is None:
            return
        target = self._find_candidate_by_id(query.message.chat.id, owner_user_id, target_user_id)
        if target is None:
            await query.edit_message_text("Этот участник больше недоступен для выбора.")
            return

        label = self._build_candidate_labels([target])[target.user_id]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "+rep",
                        callback_data=self._rep_vote_data(target_user_id, owner_user_id, "+"),
                    ),
                    InlineKeyboardButton(
                        "-rep",
                        callback_data=self._rep_vote_data(target_user_id, owner_user_id, "-"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Отмена",
                        callback_data=self._rep_cancel_data(owner_user_id),
                    )
                ],
            ]
        )
        await query.edit_message_text(
            f"Что поставить для {label}?",
            reply_markup=keyboard,
        )

    async def _apply_rep_from_callback(self, query, target_user_id: int, sign: str) -> None:
        actor = query.from_user
        message = query.message
        chat = message.chat if message is not None else None
        if actor is None or message is None or chat is None:
            return

        target = self._find_candidate_by_id(chat.id, actor.id, target_user_id)
        if target is None:
            await query.edit_message_text("Этот участник больше недоступен для выбора.")
            return

        text = self._apply_rep_vote(
            chat_id=chat.id,
            actor=actor,
            target=target,
            sign=sign,
        )
        await query.edit_message_text(text)

    async def _handle_reply_rep(self, update: Update, sign: str) -> None:
        message = update.effective_message
        actor = update.effective_user
        chat = update.effective_chat
        if message is None or actor is None or chat is None:
            return

        reply = message.reply_to_message
        target_user = reply.from_user if reply is not None else None
        if reply is None or target_user is None:
            await message.reply_text(
                "Используй эту команду ответом на сообщение участника.",
                do_quote=False,
            )
            return
        if target_user.id == actor.id:
            await message.reply_text("Себе rep ставить нельзя.", do_quote=False)
            return
        if target_user.is_bot:
            await message.reply_text("Ботам rep не ставится.", do_quote=False)
            return

        target = type("RepTarget", (), {
            "user_id": target_user.id,
            "username": target_user.username,
            "first_name": target_user.first_name,
        })()
        text = self._apply_rep_vote(
            chat_id=chat.id,
            actor=actor,
            target=target,
            sign=sign,
        )
        await message.reply_text(text, do_quote=False)

    def _apply_rep_vote(self, *, chat_id: int, actor, target, sign: str) -> str:
        now = datetime.now(self.settings.timezone)
        result = self.storage.apply_rep_vote(
            chat_id=chat_id,
            week_start=week_start_for_dt(now),
            from_user_id=actor.id,
            from_username=actor.username,
            from_first_name=actor.first_name,
            from_is_bot=actor.is_bot,
            to_user_id=target.user_id,
            to_username=target.username,
            to_first_name=target.first_name,
            to_is_bot=False,
            value=1 if sign == "+" else -1,
            voted_at=now,
        )
        target_name = self._build_candidate_labels([target])[target.user_id]
        if result == "unchanged":
            return f"У {target_name} уже стоит {sign}rep от тебя на этой неделе."
        if result == "flipped":
            return f"Голос переключён: {target_name} теперь получил {sign}rep."
        return f"Засчитано: {target_name} получил {sign}rep."

    def _build_summary_payload(self, chat_id: int, week_start, *, save_titles: bool) -> str:
        stats = self.storage.get_week_stats(chat_id, week_start)
        ranked = sort_for_ranking(stats)
        title_pairs = pick_titles(stats)
        if save_titles:
            self.storage.save_titles(chat_id, week_start, title_pairs)
        by_user_id = {item.user_id: item for item in stats}
        titled_items = [
            (by_user_id[user_id], title)
            for user_id, title in title_pairs
            if user_id in by_user_id
        ]
        return format_weekly_summary(
            week_start,
            ranked,
            titled_items,
            official=save_titles,
        )

    async def _handle_private_message(self, update: Update) -> None:
        message = update.effective_message
        if message is None:
            return
        text = (message.text or "").strip()
        if text.startswith("/start"):
            await message.reply_text(
                "Добавь меня в группу, и я начну вести рейтинг чата. В группе доступны команды: "
                "/myrating, /summary, /rep, /+rep, /-rep, /catchup и /about.",
                do_quote=False,
            )
        elif text.startswith("/about"):
            await self._send_about(update)

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
                    entity.user.is_bot,
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

    def _is_callback_owner(self, update: Update, owner_user_id: int) -> bool:
        user = update.effective_user
        return user is not None and user.id == owner_user_id

    def _find_candidate_by_id(self, chat_id: int, owner_user_id: int, target_user_id: int):
        for candidate in self.storage.list_rep_candidates(
            chat_id=chat_id,
            exclude_user_id=owner_user_id,
        ):
            if candidate.user_id == target_user_id:
                return candidate
        return None

    def _build_candidate_labels(self, candidates) -> dict[int, str]:
        base_labels: dict[int, str] = {}
        counts: Counter[str] = Counter()
        for candidate in candidates:
            label = candidate.first_name
            if candidate.username:
                label = f"{candidate.first_name} (t.me/{candidate.username})"
            base_labels[candidate.user_id] = label
            counts[label] += 1

        result: dict[int, str] = {}
        for candidate in candidates:
            label = base_labels[candidate.user_id]
            if counts[label] > 1:
                label = f"{label} #{str(candidate.user_id)[-4:]}"
            result[candidate.user_id] = label
        return result

    def _rep_pick_user_data(self, target_user_id: int, owner_user_id: int) -> str:
        return f"rep:u:{target_user_id}:{owner_user_id}"

    def _rep_vote_data(self, target_user_id: int, owner_user_id: int, sign: str) -> str:
        return f"rep:v:{target_user_id}:{owner_user_id}:{sign}"

    def _rep_cancel_data(self, owner_user_id: int) -> str:
        return f"rep:c:{owner_user_id}"

    def _maybe_capture_salute_transcript(self, update: Update) -> bool:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return False
        if not user.is_bot:
            return False
        if (user.username or "").lower() != self.settings.salute_bot_username:
            return False
        if message.reply_to_message is None:
            return False

        transcript = self._extract_salute_transcript_text(message)
        if not transcript:
            return False
        return self.storage.save_salute_transcript(
            chat_id=chat.id,
            reply_to_message_id=message.reply_to_message.message_id,
            transcript_text=transcript,
        )

    def _extract_salute_transcript_text(self, message) -> str | None:
        text = (message.text or message.caption or "").strip()
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 3 and lines[1].lower() in {"voice message", "video note"}:
            return " ".join(lines[2:]).strip()
        if len(lines) >= 2 and lines[0].lower() not in {"voice message", "video note"}:
            return " ".join(lines[1:]).strip()
        return text

    def _message_type(self, message) -> str:
        if message.voice is not None:
            return "voice"
        if message.video_note is not None:
            return "video_note"
        if message.forward_origin is not None:
            return "forward"
        if message.caption:
            return "caption"
        if message.text:
            return "text"
        return "other"

    def _message_text_content(self, message) -> str | None:
        text = (message.text or message.caption or "").strip()
        return text or None

    def _message_file_id(self, message) -> str | None:
        if message.voice is not None:
            return message.voice.file_id
        if message.video_note is not None:
            return message.video_note.file_id
        return None

    def _transcript_status_for_message(self, message) -> str:
        if message.voice is not None or message.video_note is not None:
            return "missing"
        return "not_needed"

    def _message_row_to_summary_block(self, row) -> str | None:
        name = row["first_name"]
        if row["username"]:
            name = f"{row['first_name']} (t.me/{row['username']})"
        message_type = row["message_type"]
        if message_type in {"voice", "video_note"}:
            transcript = row["transcript_text"]
            if not transcript:
                return None
            prefix = "[кружочек]" if message_type == "video_note" else "[голосовое]"
            return f"{name}: {prefix} {transcript}"
        text = (row["text_content"] or "").strip()
        if not text:
            return None
        if message_type == "forward":
            text = f"[переслано] {text}"
        return f"{name}: {text}"
