from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from bot.ai import SummaryError, analyze_players, generate_quip
from bot.config import Settings
from bot.stats import (
    compute_current_streaks,
    format_kd,
    format_kill_matrix,
    format_monthly_recap,
    format_table,
    league_rank,
    pick_titles,
    sort_by_league_points,
    sort_players,
)
from bot.storage import Storage

logger = logging.getLogger(__name__)

GAME_LABELS = {"pubg": "PUBG", "cs": "CS"}
TEAM_ICONS = {1: "🔴", 2: "🔵", 3: "🟢", 4: "🟡", 5: "🟣", 6: "🟠", 7: "⚫", 8: "⚪"}
TEAM_NAMES = {
    1: "Красные", 2: "Синие", 3: "Зелёные", 4: "Жёлтые",
    5: "Фиолетовые", 6: "Оранжевые", 7: "Чёрные", 8: "Белые",
}
MAX_TEAMS = 8


def _team_icon(team: int | None) -> str:
    return TEAM_ICONS.get(team, "⬜")


def _team_name(team: int | None) -> str:
    return TEAM_NAMES.get(team, f"Команда {team}")


PENDING_FALLBACK_WINDOW = timedelta(seconds=90)
SETTINGS_LABELS = {
    "quips": "🤖 Подколы от GPT",
    "predictions": "🔮 Прогнозы перед турниром",
    "mvp": "🗳 MVP-голосование",
}

COMMAND_MENU = re.compile(r"^/(бот|menu)(?:@\w+)?$")
COMMAND_TABLE = re.compile(r"^/(таблица|table)(?:@\w+)?$")
COMMAND_MATCH = re.compile(r"^/(матч|match)(?:@\w+)?$")
COMMAND_LEAGUE = re.compile(r"^/(лига|league)(?:@\w+)?$")
COMMAND_ANALYZE = re.compile(r"^/(разбор|analyze)(?:@\w+)?$")
COMMAND_ABOUT = re.compile(r"^/(about|start)(?:@\w+)?$")


class BotHandlers:
    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings

    # -- top-level routing -------------------------------------------------

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
        if user.is_bot:
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
        self._register_incidental_users(message, chat.id, now)

        text = (message.text or "").strip()
        if not text:
            return

        if not text.startswith("/"):
            await self._handle_pending_text(update)
            return

        if COMMAND_MENU.match(text):
            await self._send_menu(update)
        elif COMMAND_TABLE.match(text):
            await self._send_table_command(update)
        elif COMMAND_MATCH.match(text):
            await self._send_match_command(update)
        elif COMMAND_LEAGUE.match(text):
            await self._send_league_command(update)
        elif COMMAND_ANALYZE.match(text):
            await self._send_analyze_command(update)
        elif COMMAND_ABOUT.match(text):
            await self._send_about(update)

    async def on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.message is None:
            return
        chat = query.message.chat
        actor = query.from_user
        if actor is not None:
            self.storage.register_chat_presence(
                chat_id=chat.id,
                user_id=actor.id,
                username=actor.username,
                first_name=actor.first_name,
                is_bot=actor.is_bot,
                seen_at=datetime.now(self.settings.timezone).date(),
            )

        parts = query.data.split(":")
        prefix = parts[0]
        try:
            if prefix == "mn":
                await self._handle_menu_callback(query, chat.id, parts, context)
            elif prefix == "tn":
                await self._handle_setup_callback(query, chat.id, parts)
            elif prefix == "mt":
                await self._handle_match_callback(query, chat.id, parts, context)
            elif prefix == "pl":
                await self._handle_players_callback(query, chat.id, parts)
            elif prefix == "st":
                await self._handle_settings_callback(query, chat.id, parts)
            elif prefix == "tl":
                await self._handle_tournaments_callback(query, chat.id, parts)
            elif prefix == "pr":
                await self._handle_prediction_callback(query, parts)
            elif prefix == "mv":
                await self._handle_mvp_callback(query, parts)
            elif prefix == "ap":
                await self._handle_add_player_callback(query, chat.id, parts)
            else:
                await query.answer()
        except Exception:
            logger.exception("callback failed data=%s", query.data)
            await query.answer("Что-то пошло не так, попробуй ещё раз.", show_alert=True)

    async def _handle_private_message(self, update: Update) -> None:
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            "Привет! Добавь меня в группу — там я веду турнирную таблицу PUBG/CS. "
            "В группе команда /menu открывает меню.",
            do_quote=False,
        )

    def _localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.settings.timezone)

    def _register_incidental_users(self, message, chat_id: int, now: datetime) -> None:
        """Registers people the bot learns about without them posting themselves:
        whoever a message replies to, and anyone tagged via a clickable text-mention."""
        candidates = []
        reply_user = message.reply_to_message.from_user if message.reply_to_message else None
        if reply_user is not None:
            candidates.append(reply_user)
        for entity in message.entities or ():
            if entity.type == MessageEntity.TEXT_MENTION and entity.user is not None:
                candidates.append(entity.user)

        for candidate in candidates:
            if candidate.is_bot:
                continue
            self.storage.register_chat_presence(
                chat_id=chat_id,
                user_id=candidate.id,
                username=candidate.username,
                first_name=candidate.first_name,
                is_bot=candidate.is_bot,
                seen_at=now.date(),
            )

    # -- pending free-text input -------------------------------------------

    async def _send_force_reply_prompt(self, message, user, ask_text: str, placeholder: str):
        """Mentions the user so Telegram's `selective` ForceReply targets them specifically —
        without a mention or an existing reply-to-them, selective ForceReply activates for
        no one, which is why plain typing silently did nothing before this fix."""
        mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'
        return await message.reply_text(
            f"{mention}, {ask_text}",
            parse_mode="HTML",
            do_quote=False,
            reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        )

    def _pending_is_fresh(self, pending) -> bool:
        created_at = datetime.fromisoformat(pending["created_at"])
        return datetime.now(timezone.utc) - created_at <= PENDING_FALLBACK_WINDOW

    async def _handle_pending_text(self, update: Update) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or message.text is None:
            return
        pending = self.storage.get_pending_input(chat_id=chat.id, user_id=user.id)
        if pending is None:
            return

        # Answering by replying to our prompt always counts. If that didn't happen
        # (e.g. ForceReply didn't kick in on some client), a plain message from the
        # same person still counts, but only within a short window — long enough to
        # cover a slow typer, short enough that unrelated later chatter isn't swallowed.
        reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
        is_direct_reply = reply_to_id == pending["prompt_message_id"]
        if not is_direct_reply and not self._pending_is_fresh(pending):
            return

        self.storage.clear_pending_input(chat_id=chat.id, user_id=user.id)
        kind = pending["kind"]
        try:
            await message.get_bot().delete_message(chat_id=chat.id, message_id=pending["prompt_message_id"])
        except Exception:
            pass

        if kind == "tn_name":
            draft = self.storage.get_setup_draft(chat.id)
            if draft is None:
                return
            name = message.text.strip()[:60] or "Без названия"
            self.storage.set_draft_name(draft["tournament_id"], name)
            draft = self.storage.get_tournament(draft["tournament_id"])
            text, kb = self._render_wizard_step(draft, chat.id)
            await message.reply_text(f"✅ Название: {name}\n\n{text}", reply_markup=kb, do_quote=False)
            return

        if kind.startswith("add_player:"):
            origin = kind.split(":", 1)[1]
            raw = message.text.strip()
            names = [n.strip() for n in raw.split(",") if n.strip()] if "," in raw else ([raw] if raw else [])
            names = names[:5]
            for name in names:
                self.storage.add_guest_player(
                    chat_id=chat.id, first_name=name, seen_at=self._localized(message.date).date()
                )
            if not names:
                return
            confirmation = f"✅ Добавлен{'ы' if len(names) > 1 else ''}: {', '.join(names)}"
            if origin == "setup":
                draft = self.storage.get_setup_draft(chat.id)
                if draft is not None:
                    text, kb = self._render_wizard_step(draft, chat.id)
                    await message.reply_text(f"{confirmation}\n\n{text}", reply_markup=kb, do_quote=False)
            elif origin == "roster":
                tournament = self.storage.get_active_tournament(chat.id)
                if tournament is not None:
                    live = self.storage.get_live_match(tournament["tournament_id"])
                    if live is not None:
                        text, kb = self._render_match_roster(live["match_id"], chat.id)
                        await message.reply_text(f"{confirmation}\n\n{text}", reply_markup=kb, do_quote=False)
            else:
                text, kb = self._render_players_screen(chat.id)
                await message.reply_text(f"{confirmation}\n\n{text}", reply_markup=kb, do_quote=False)

    # -- command shortcuts ---------------------------------------------------

    async def _send_menu(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        text, kb = self._render_main_menu(chat.id)
        await message.reply_text(text, reply_markup=kb, do_quote=False)

    async def _send_table_command(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        text, kb = self._render_table_view(chat.id)
        await message.reply_text(text, reply_markup=kb, parse_mode="HTML", do_quote=False)

    async def _send_match_command(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        tournament = self.storage.get_active_tournament(chat.id)
        if tournament is None:
            await message.reply_text(
                "Сейчас турнир не идёт. Открой /menu → 🏆 Новый турнир.", do_quote=False
            )
            return
        live = self.storage.get_live_match(tournament["tournament_id"])
        if live is None:
            await message.reply_text("Матч не найден.", do_quote=False)
            return
        text, kb = self._render_scoreboard(live["match_id"])
        await message.reply_text(text, reply_markup=kb, parse_mode="HTML", do_quote=False)

    async def _send_league_command(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        text, kb = self._render_league_view(chat.id)
        await message.reply_text(text, reply_markup=kb, do_quote=False)

    async def _send_analyze_command(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        status = await message.reply_text("⏳ Считаю разбор...", do_quote=False)
        await self._run_analyze(chat.id, status)

    async def _send_about(self, update: Update) -> None:
        message = update.effective_message
        await message.reply_text(self._about_text(), do_quote=False)

    def _about_text(self) -> str:
        return (
            "🎮 Как этим пользоваться\n\n"
            "1. /menu — открыть меню\n"
            "2. 🏆 Новый турнир — выбрать игру, режим и кто играет (один раз в начале вечера)\n"
            "3. Дальше во время игры — только кнопки: ➕ засчитывает убийство и спросит, кого убили; "
            "🏆 — взял топ (или победа команды по цвету, если играете 2-4 командами)\n"
            "4. ✅ Матч сыгран — бот публикует таблицу и сразу открывает следующий матч с тем же составом\n"
            "5. 🏁 Завершить турнир — итоговая таблица, кто кого убивал, титулы и разбор от GPT\n\n"
            "Если запутался — просто набери /menu, там всё видно кнопками."
        )

    # -- main menu -----------------------------------------------------------

    def _render_main_menu(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        active = self.storage.get_active_tournament(chat_id)
        draft = self.storage.get_setup_draft(chat_id)
        rows: list[list[InlineKeyboardButton]] = []
        if active is not None:
            rows.append([InlineKeyboardButton("▶️ Вернуться к матчу", callback_data="mn:resume")])
        elif draft is not None:
            rows.append([InlineKeyboardButton("▶️ Продолжить настройку", callback_data="mn:new")])
        else:
            rows.append([InlineKeyboardButton("🏆 Новый турнир", callback_data="mn:new")])
        rows.append(
            [
                InlineKeyboardButton("📊 Таблица", callback_data="mn:table"),
                InlineKeyboardButton("👥 Игроки", callback_data="mn:players"),
            ]
        )
        rows.append([InlineKeyboardButton("📜 История", callback_data="mn:history")])
        return "🎮 Турнирный бот", InlineKeyboardMarkup(rows)

    def _back_to_menu_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="mn:home")]])

    async def _handle_menu_callback(self, query, chat_id: int, parts: list[str], context) -> None:
        action = parts[1]
        if action == "home":
            text, kb = self._render_main_menu(chat_id)
            await query.answer()
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "new":
            await self._start_or_resume_setup(query, chat_id)
        elif action == "resume":
            tournament = self.storage.get_active_tournament(chat_id)
            if tournament is None:
                await query.answer("Турнир не идёт.", show_alert=True)
                return
            live = self.storage.get_live_match(tournament["tournament_id"])
            await query.answer()
            if live is not None:
                text, kb = self._render_scoreboard(live["match_id"])
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        elif action == "table":
            await query.answer()
            text, kb = self._render_table_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        elif action == "history":
            await query.answer()
            text, kb = self._render_history_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "undo":
            tournament = self._resolve_view_target(chat_id)
            if tournament is None:
                await query.answer("Нечего отменять.", show_alert=True)
                return
            ok = self.storage.undo_last_saved_match(tournament["tournament_id"])
            await query.answer("Матч отменён." if ok else "Нет сохранённых матчей.")
            text, kb = self._render_history_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "league":
            await query.answer()
            text, kb = self._render_league_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "analyze":
            await query.answer()
            await query.edit_message_text("⏳ Считаю разбор...")
            await self._run_analyze(chat_id, query.message)
        elif action == "tournaments":
            await query.answer()
            text, kb = self._render_tournaments_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "players":
            await query.answer()
            text, kb = self._render_players_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        elif action == "settings":
            await query.answer()
            text, kb = self._render_settings_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await query.answer()

    def _resolve_view_target(self, chat_id: int):
        active = self.storage.get_active_tournament(chat_id)
        if active is not None:
            return active
        finished = self.storage.list_finished_tournaments(chat_id, limit=1)
        return finished[0] if finished else None

    def _table_hub_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎖 Лига", callback_data="mn:league"),
                    InlineKeyboardButton("🤖 Разбор", callback_data="mn:analyze"),
                ],
                [InlineKeyboardButton("⚙️ Настройки", callback_data="mn:settings")],
                [InlineKeyboardButton("← Меню", callback_data="mn:home")],
            ]
        )

    def _render_table_view(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        tournament = self._resolve_view_target(chat_id)
        if tournament is None:
            return "Пока не было ни одного турнира. Нажми «🏆 Новый турнир».", self._back_to_menu_kb()
        totals = self.storage.get_tournament_totals(tournament["tournament_id"])
        text = format_table(
            tournament_name=tournament["name"],
            game_label=GAME_LABELS.get(tournament["game"], tournament["game"]),
            players=totals,
        )
        return text, self._table_hub_kb()

    def _render_history_view(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        tournament = self._resolve_view_target(chat_id)
        if tournament is None:
            return "Пока нет истории матчей.", self._back_to_menu_kb()
        matches = self.storage.list_recent_matches(tournament["tournament_id"], limit=10)
        if not matches:
            text = "В этом турнире ещё нет сыгранных матчей."
        else:
            lines = [f"📜 История · {tournament['name']}", ""]
            for m in matches:
                kills = self.storage.get_match_kills(m["match_id"])
                by_killer: dict[str, list[str]] = {}
                for k in kills:
                    by_killer.setdefault(k["killer_name"], []).append(k["victim_name"] or "случайного")
                kill_text = "; ".join(f"{name} убил {', '.join(v)}" for name, v in by_killer.items())
                if tournament["team_mode"]:
                    winner = f"{_team_icon(m['winner_team'])} {_team_name(m['winner_team'])}" if m["winner_team"] else "—"
                else:
                    players = self.storage.get_match_players(m["match_id"])
                    winners = [p["first_name"] for p in players if p["top"] == 0]
                    winner = ", ".join(winners) if winners else "—"
                line = f"#{m['match_no']} · 🏆 {winner}"
                if kill_text:
                    line += f" · {kill_text}"
                lines.append(line)
            text = "\n".join(lines)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("↩️ Отменить последний матч", callback_data="mn:undo")],
                [InlineKeyboardButton("← Меню", callback_data="mn:home")],
            ]
        )
        return text, kb

    def _render_league_view(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗂 Прошлые турниры", callback_data="mn:tournaments")],
                [InlineKeyboardButton("← Меню", callback_data="mn:home")],
            ]
        )
        totals = self.storage.get_league_totals(chat_id)
        if not totals:
            return "Лига пока пуста — заверши хотя бы один турнир.", kb
        ranked = sort_by_league_points(totals)
        finished_count = len(self.storage.list_finished_tournaments(chat_id, limit=1000))
        lines = [f"🎖 Лига · {finished_count} турниров", ""]
        for i, p in enumerate(ranked, start=1):
            rank = league_rank(p.league_points)
            lines.append(
                f"{i}. {rank} {p.first_name}   {p.league_points} · {p.tops} побед · "
                f"{p.kills} убийств · KD {format_kd(p.kd)}"
            )
        return "\n".join(lines), kb

    async def post_monthly_recap(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Scheduled for the 1st of each month (see bot/main.py) — posts a recap of the
        previous calendar month to every chat that has finished at least one tournament in it."""
        now = datetime.now(self.settings.timezone)
        prev = now.replace(day=1) - timedelta(days=1)
        for chat_id in self.storage.list_known_chat_ids():
            totals, tournament_count, match_count = self.storage.get_month_stats(chat_id, prev.year, prev.month)
            text = format_monthly_recap(
                month=prev.month, players=totals, tournament_count=tournament_count, match_count=match_count
            )
            if text is None:
                continue
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("Failed to post monthly recap to chat %s", chat_id)

    # -- tournament setup (step by step) --------------------------------------

    async def _start_or_resume_setup(self, query, chat_id: int) -> None:
        draft = self.storage.get_setup_draft(chat_id)
        if draft is None:
            active = self.storage.get_active_tournament(chat_id)
            if active is not None:
                await query.answer("Турнир уже идёт. Сначала заверши текущий.", show_alert=True)
                return
            default_name = datetime.now(self.settings.timezone).strftime("%d.%m")
            tournament_id = self.storage.create_setup_draft(
                chat_id=chat_id, created_by=query.from_user.id, default_name=default_name
            )
            draft = self.storage.get_tournament(tournament_id)
        await query.answer()
        text, kb = self._render_wizard_step(draft, chat_id)
        await query.edit_message_text(text, reply_markup=kb)

    def _render_wizard_step(self, draft, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        if not draft["game"]:
            return self._render_game_step(draft)
        if draft["team_mode"] is None:
            return self._render_mode_step(draft)
        if draft["team_mode"] == 0:
            return self._render_solo_roster_step(chat_id, draft)
        return self._render_team_roster_step(chat_id, draft, draft["team_step"] or 1)

    def _render_game_step(self, draft) -> tuple[str, InlineKeyboardMarkup]:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("PUBG", callback_data="tn:game:pubg"),
                    InlineKeyboardButton("CS", callback_data="tn:game:cs"),
                ],
                [
                    InlineKeyboardButton("✏️ Название", callback_data="tn:name:custom"),
                    InlineKeyboardButton("🗑 Отмена", callback_data="tn:cancel"),
                ],
            ]
        )
        return f"🏆 {draft['name']}\n\nИгра?", kb

    def _render_mode_step(self, draft) -> tuple[str, InlineKeyboardMarkup]:
        game_label = GAME_LABELS.get(draft["game"], draft["game"])
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Соло — каждый сам за себя", callback_data="tn:mode:solo")],
                [InlineKeyboardButton("Командами", callback_data="tn:mode:team")],
                [InlineKeyboardButton("🗑 Отмена", callback_data="tn:cancel")],
            ]
        )
        return f"🏆 {draft['name']} · {game_label}\n\nКак играем?", kb

    def _render_solo_roster_step(self, chat_id: int, draft) -> tuple[str, InlineKeyboardMarkup]:
        tournament_id = draft["tournament_id"]
        all_players = self.storage.list_players(chat_id)
        selected = {r["user_id"] for r in self.storage.get_draft_players(tournament_id)}

        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for p in all_players:
            label = f"{'✅' if p.user_id in selected else '⬜'} {p.first_name}"
            line.append(InlineKeyboardButton(label, callback_data=f"tn:p:{p.user_id}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([InlineKeyboardButton("➕ Игрок", callback_data="ap:setup")])
        rows.append(
            [
                InlineKeyboardButton("🚀 Начать", callback_data="tn:start"),
                InlineKeyboardButton("🗑 Отмена", callback_data="tn:cancel"),
            ]
        )

        game_label = GAME_LABELS.get(draft["game"], draft["game"])
        text = f"🏆 {draft['name']} · {game_label} · Соло\n\nКто играет? Тапай, чтобы включить/выключить."
        if not all_players:
            text += "\n\nПока никого не знаю — напиши что-нибудь в чат или добавь игрока вручную."
        return text, InlineKeyboardMarkup(rows)

    def _render_team_roster_step(self, chat_id: int, draft, team_step: int) -> tuple[str, InlineKeyboardMarkup]:
        tournament_id = draft["tournament_id"]
        assigned = self.storage.get_draft_players(tournament_id)
        locked_elsewhere = {p["user_id"] for p in assigned if p["team"] != team_step}
        selected_here = {p["user_id"] for p in assigned if p["team"] == team_step}
        candidates = [p for p in self.storage.list_players(chat_id) if p.user_id not in locked_elsewhere]

        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for p in candidates:
            label = f"{'✅' if p.user_id in selected_here else '⬜'} {p.first_name}"
            line.append(InlineKeyboardButton(label, callback_data=f"tn:p:{p.user_id}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([InlineKeyboardButton("➕ Игрок", callback_data="ap:setup")])

        nav_row = []
        if team_step < MAX_TEAMS:
            nav_row.append(InlineKeyboardButton(f"▶️ Команда {team_step + 1}", callback_data="tn:team_next"))
        if team_step >= 2:
            nav_row.append(InlineKeyboardButton("🚀 Начать", callback_data="tn:start"))
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineKeyboardButton("🗑 Отмена", callback_data="tn:cancel")])

        game_label = GAME_LABELS.get(draft["game"], draft["game"])
        text = (
            f"🏆 {draft['name']} · {game_label} · Командами\n\n"
            f"{_team_icon(team_step)} Команда {team_step}: кто играет?"
        )
        if not candidates:
            text += "\n\nБольше некого позвать — все уже в других командах. Можно добавить нового игрока."
        return text, InlineKeyboardMarkup(rows)

    async def _handle_setup_callback(self, query, chat_id: int, parts: list[str]) -> None:
        draft = self.storage.get_setup_draft(chat_id)
        if draft is None:
            await query.answer("Настройка турнира уже не активна.", show_alert=True)
            return
        tournament_id = draft["tournament_id"]
        action = parts[1]

        if action == "name":
            await query.answer()
            prompt = await self._send_force_reply_prompt(
                query.message, query.from_user, "напиши название турнира.", "Название турнира"
            )
            self.storage.set_pending_input(
                chat_id=chat_id, user_id=query.from_user.id, kind="tn_name", prompt_message_id=prompt.message_id
            )
            return

        if action == "game":
            self.storage.set_draft_game(tournament_id, parts[2])
            await query.answer()
            draft = self.storage.get_tournament(tournament_id)
            text, kb = self._render_wizard_step(draft, chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "mode":
            if parts[2] == "solo":
                self.storage.set_draft_mode(tournament_id, 0)
            else:
                self.storage.set_draft_mode(tournament_id, -1)
                self.storage.set_draft_team_step(tournament_id, 1)
            await query.answer()
            draft = self.storage.get_tournament(tournament_id)
            text, kb = self._render_wizard_step(draft, chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "p":
            user_id = int(parts[2])
            team_step = 0 if draft["team_mode"] == 0 else (draft["team_step"] or 1)
            self.storage.toggle_draft_player(tournament_id, user_id, team_step)
            await query.answer()
            draft = self.storage.get_tournament(tournament_id)
            text, kb = self._render_wizard_step(draft, chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "team_next":
            current_step = draft["team_step"] or 1
            has_someone = any(p["team"] == current_step for p in self.storage.get_draft_players(tournament_id))
            if not has_someone:
                await query.answer("Добавь хотя бы одного игрока в эту команду.", show_alert=True)
                return
            self.storage.advance_draft_team_step(tournament_id)
            await query.answer()
            draft = self.storage.get_tournament(tournament_id)
            text, kb = self._render_wizard_step(draft, chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "start":
            await self._start_tournament(query, chat_id, draft)
            return

        if action == "cancel":
            self.storage.cancel_draft(tournament_id)
            await query.answer("Отменено.")
            text, kb = self._render_main_menu(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

    async def _start_tournament(self, query, chat_id: int, draft) -> None:
        tournament_id = draft["tournament_id"]
        is_team = draft["team_mode"] != 0
        players = self.storage.get_draft_players(tournament_id)

        if is_team:
            distinct_teams = sorted({p["team"] for p in players if p["team"] > 0})
            if len(distinct_teams) < 2:
                await query.answer("Раздели игроков минимум на 2 команды — тапай по имени.", show_alert=True)
                return
            self.storage.set_draft_mode(tournament_id, max(distinct_teams))
        elif len(players) < 2:
            await query.answer("Нужно минимум два игрока.", show_alert=True)
            return

        match_id = self.storage.start_tournament(tournament_id)
        tournament = self.storage.get_tournament(tournament_id)
        await query.answer()
        await query.edit_message_text(f"🚀 Турнир «{tournament['name']}» начат!")

        if self.storage.get_setting(chat_id, "predictions") and len(players) >= 2:
            candidates = [SimpleNamespace(user_id=p["user_id"], first_name=p["first_name"]) for p in players]
            text, kb = self._render_prediction_poll(tournament_id, candidates)
            await query.message.reply_text(text, reply_markup=kb, do_quote=False)

        text, kb = self._render_scoreboard(match_id)
        await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML", do_quote=False)

    # -- live match scoreboard -----------------------------------------------

    def _render_scoreboard(self, match_id: int) -> tuple[str, InlineKeyboardMarkup]:
        match = self.storage.get_match(match_id)
        tournament = self.storage.get_tournament(match["tournament_id"])
        all_players = sorted(self.storage.get_match_players(match_id), key=lambda r: r["first_name"])
        alive = [p for p in all_players if p["top"] == 0]
        game_label = GAME_LABELS.get(tournament["game"], tournament["game"])
        mode_label = "командами" if tournament["team_mode"] else "соло"
        header = f"🏆 {tournament['name']} · {game_label} {mode_label}\nМатч #{match['match_no']}"

        team_count = tournament["team_mode"]
        rows: list[list[InlineKeyboardButton]] = []
        if team_count == 0:
            lines = [f"{p['first_name']:<12} {p['kills']} убийств" for p in alive]
        else:
            lines = []
            for t in range(1, team_count + 1):
                team_players = [p for p in alive if p["team"] == t]
                if not team_players:
                    continue
                lines.append(f"{_team_icon(t)} {_team_name(t)}")
                lines += [f"{p['first_name']:<12} {p['kills']} убийств" for p in team_players]
                lines.append("")
            if lines:
                lines.pop()  # drop trailing blank line

        for p in alive:
            rows.append(
                [
                    InlineKeyboardButton(f"➕ {p['first_name']}", callback_data=f"mt:kill:{p['user_id']}"),
                    InlineKeyboardButton("−", callback_data=f"mt:died:{p['user_id']}"),
                ]
            )
        rows.append([InlineKeyboardButton("✅ Матч сыгран", callback_data="mt:save")])
        rows.append([InlineKeyboardButton("↩️ Отменить последнее", callback_data="mt:undo_last")])
        rows.append(
            [
                InlineKeyboardButton("👥 Состав", callback_data="mt:roster"),
                InlineKeyboardButton("🏁 Завершить турнир", callback_data="mt:finish"),
            ]
        )
        body = "<pre>" + "\n".join(lines) + "</pre>" if lines else "Все выбыли."
        return f"{header}\n\n{body}", InlineKeyboardMarkup(rows)

    def _render_victim_picker(self, match_id: int, killer_id: int) -> tuple[str, InlineKeyboardMarkup]:
        match = self.storage.get_match(match_id)
        tournament = self.storage.get_tournament(match["tournament_id"])
        players = self.storage.get_match_players(match_id)
        killer = next((p for p in players if p["user_id"] == killer_id), None)
        if killer is None or killer["top"] == 1:
            return self._render_scoreboard(match_id)
        alive = [p for p in players if p["top"] == 0]
        if tournament["team_mode"]:
            candidates = [p for p in alive if p["team"] != killer["team"]]
        else:
            candidates = [p for p in alive if p["user_id"] != killer_id]

        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for c in candidates:
            line.append(InlineKeyboardButton(c["first_name"], callback_data=f"mt:victim:{killer_id}:{c['user_id']}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([InlineKeyboardButton("🎲 Рандома / не из наших", callback_data=f"mt:victim:{killer_id}:0")])
        rows.append([InlineKeyboardButton("← Назад", callback_data="mt:back")])

        team_note = f" ({_team_icon(killer['team'])})" if tournament["team_mode"] else ""
        return f"Кого убил {killer['first_name']}{team_note}?", InlineKeyboardMarkup(rows)

    def _render_killer_picker(self, match_id: int, victim_id: int) -> tuple[str, InlineKeyboardMarkup]:
        match = self.storage.get_match(match_id)
        tournament = self.storage.get_tournament(match["tournament_id"])
        players = self.storage.get_match_players(match_id)
        victim = next((p for p in players if p["user_id"] == victim_id), None)
        if victim is None or victim["top"] == 1:
            return self._render_scoreboard(match_id)
        alive = [p for p in players if p["top"] == 0]
        if tournament["team_mode"]:
            candidates = [p for p in alive if p["team"] != victim["team"]]
        else:
            candidates = [p for p in alive if p["user_id"] != victim_id]

        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for c in candidates:
            line.append(InlineKeyboardButton(c["first_name"], callback_data=f"mt:killer:{victim_id}:{c['user_id']}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([InlineKeyboardButton("🌀 Зона / падение", callback_data=f"mt:zone:{victim_id}")])
        rows.append([InlineKeyboardButton("← Назад", callback_data="mt:back")])

        team_note = f" ({_team_icon(victim['team'])})" if tournament["team_mode"] else ""
        return f"От кого умер {victim['first_name']}{team_note}?", InlineKeyboardMarkup(rows)

    def _render_match_roster(self, match_id: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        match = self.storage.get_match(match_id)
        tournament = self.storage.get_tournament(match["tournament_id"])
        team_count = tournament["team_mode"]
        all_players = self.storage.list_players(chat_id)
        current = {p["user_id"]: p["team"] for p in self.storage.get_match_players(match_id)}
        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for p in all_players:
            team = current.get(p.user_id)
            if team_count == 0:
                label = f"{'✅' if team is not None else '⬜'} {p.first_name}"
            else:
                label = f"{_team_icon(team)} {p.first_name}"
            line.append(InlineKeyboardButton(label, callback_data=f"mt:roster:p:{p.user_id}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([InlineKeyboardButton("➕ Добавить игрока", callback_data="ap:roster")])
        if team_count and team_count < MAX_TEAMS:
            rows.append([InlineKeyboardButton("➕ Новая команда", callback_data="mt:roster:new_team")])
        rows.append([InlineKeyboardButton("✅ Готово", callback_data="mt:roster:done")])
        text = "Кто играет сейчас?"
        if team_count:
            text += " Тапай по имени, чтобы переключить между командами."
        return text, InlineKeyboardMarkup(rows)

    def _auto_set_team_winner(self, match_id: int, tournament) -> None:
        """Called right before saving a team match: if exactly one team still has a
        survivor, that's the winner. Ambiguous (0 or 2+ teams alive — the match was
        ended early) leaves winner_team unset, same as forgetting to pick one before."""
        if not tournament["team_mode"]:
            return
        players = self.storage.get_match_players(match_id)
        alive_teams = {p["team"] for p in players if p["top"] == 0}
        if len(alive_teams) == 1:
            self.storage.set_winner_team(match_id=match_id, team=next(iter(alive_teams)))

    def _build_match_recap(self, match_id: int) -> str:
        match = self.storage.get_match(match_id)
        tournament = self.storage.get_tournament(match["tournament_id"])
        players = self.storage.get_match_players(match_id)
        kills = self.storage.get_match_kills(match_id)

        if tournament["team_mode"]:
            winner = (
                f"{_team_icon(match['winner_team'])} {_team_name(match['winner_team'])}"
                if match["winner_team"]
                else "нет победителя"
            )
            header = f"Матч #{match['match_no']} · 🏆 {winner}"
        else:
            winners = [p["first_name"] for p in players if p["top"] == 0]
            header = f"Матч #{match['match_no']} · 🏆 {', '.join(winners) if winners else 'нет победителя'}"

        by_killer: dict[str, list[str]] = {}
        for k in kills:
            by_killer.setdefault(k["killer_name"], []).append(k["victim_name"] or "случайного")
        kill_text = " · ".join(f"{name} убил {', '.join(v)}" for name, v in by_killer.items())

        names = {p["user_id"]: p["first_name"] for p in players}
        streaks = compute_current_streaks(self.storage.get_match_results_sequence(match["tournament_id"]))
        streak_text = " · ".join(
            f"{icon} {names[uid]}: {length} подряд" for uid, (icon, length) in streaks.items() if uid in names
        )

        parts = [header]
        if kill_text:
            parts.append(kill_text)
        if streak_text:
            parts.append(streak_text)
        return "\n".join(parts)

    async def _handle_match_callback(self, query, chat_id: int, parts: list[str], context) -> None:
        action = parts[1]

        if action == "back":
            tournament = self.storage.get_active_tournament(chat_id)
            live = self.storage.get_live_match(tournament["tournament_id"]) if tournament else None
            if live is None:
                await query.answer()
                return
            await query.answer()
            text, kb = self._render_scoreboard(live["match_id"])
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        tournament = self.storage.get_active_tournament(chat_id)
        if tournament is None:
            await query.answer("Турнир уже не активен.", show_alert=True)
            return
        live = self.storage.get_live_match(tournament["tournament_id"])
        if live is None:
            await query.answer("Матч уже не активен.", show_alert=True)
            return
        match_id = live["match_id"]

        if action == "kill":
            killer_id = int(parts[2])
            await query.answer()
            text, kb = self._render_victim_picker(match_id, killer_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "victim":
            killer_id, victim_id = int(parts[2]), int(parts[3])
            self.storage.record_kill(match_id=match_id, killer_id=killer_id, victim_id=victim_id or None)
            await query.answer("Убийство засчитано.")
            text, kb = self._render_scoreboard(match_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if action == "died":
            victim_id = int(parts[2])
            await query.answer()
            text, kb = self._render_killer_picker(match_id, victim_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

        if action == "killer":
            victim_id, killer_id = int(parts[2]), int(parts[3])
            self.storage.record_kill(match_id=match_id, killer_id=killer_id, victim_id=victim_id)
            await query.answer("Записано.")
            text, kb = self._render_scoreboard(match_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if action == "zone":
            victim_id = int(parts[2])
            self.storage.record_kill(match_id=match_id, killer_id=0, victim_id=victim_id)
            await query.answer("Отмечено: выбыл без убийцы.")
            text, kb = self._render_scoreboard(match_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if action == "undo_last":
            undone = self.storage.undo_last_action(match_id=match_id)
            await query.answer("Отменено." if undone is not None else "Нечего отменять.")
            text, kb = self._render_scoreboard(match_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if action == "roster":
            if len(parts) == 2:
                await query.answer()
                text, kb = self._render_match_roster(match_id, chat_id)
                await query.edit_message_text(text, reply_markup=kb)
                return
            if parts[2] == "p":
                user_id = int(parts[3])
                self.storage.toggle_match_roster(match_id, user_id, tournament["team_mode"])
                await query.answer()
                text, kb = self._render_match_roster(match_id, chat_id)
                await query.edit_message_text(text, reply_markup=kb)
                return
            if parts[2] == "new_team":
                new_team = self.storage.add_tournament_team(tournament["tournament_id"])
                await query.answer(f"Добавлена команда {new_team}.")
                text, kb = self._render_match_roster(match_id, chat_id)
                await query.edit_message_text(text, reply_markup=kb)
                return
            if parts[2] == "done":
                await query.answer()
                text, kb = self._render_scoreboard(match_id)
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
                return

        if action == "save":
            self._auto_set_team_winner(match_id, tournament)
            new_match_id = self.storage.save_match_and_advance(
                tournament_id=tournament["tournament_id"], match_id=match_id
            )
            recap = self._build_match_recap(match_id)
            await query.answer("Матч сохранён.")
            await query.edit_message_text(recap)

            totals = self.storage.get_tournament_totals(tournament["tournament_id"])
            table_text = format_table(
                tournament_name=tournament["name"],
                game_label=GAME_LABELS.get(tournament["game"], tournament["game"]),
                players=totals,
            )
            await query.message.reply_text(table_text, parse_mode="HTML", do_quote=False)

            text, kb = self._render_scoreboard(new_match_id)
            await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML", do_quote=False)

            if self.settings.openai_api_key and self.storage.get_setting(chat_id, "quips"):
                asyncio.create_task(self._post_quip(chat_id, context.bot, recap))
            return

        if action == "finish":
            if len(parts) == 2:
                saved = self.storage.list_recent_matches(tournament["tournament_id"], limit=1000)
                await query.answer()
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Да, завершить", callback_data="mt:finish:yes"),
                            InlineKeyboardButton("← Отмена", callback_data="mt:finish:no"),
                        ]
                    ]
                )
                prompt = f"Точно завершить турнир? Сыграно матчей: {len(saved)}."
                if self.storage.match_has_progress(match_id):
                    prompt += (
                        f"\n\n⚠️ Текущий матч #{live['match_no']} ещё не сохранён — "
                        "если завершить турнир сейчас, его убийства и топы пропадут. "
                        "Сначала нажми «✅ Матч сыгран», если хочешь его сохранить."
                    )
                await query.edit_message_text(prompt, reply_markup=kb)
                return
            if parts[2] == "no":
                await query.answer()
                text, kb = self._render_scoreboard(match_id)
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
                return
            if parts[2] == "yes":
                await query.answer()
                await self._finish_tournament_flow(query, tournament)
                return

    async def _post_quip(self, chat_id: int, bot, context_text: str) -> None:
        try:
            quip = await asyncio.to_thread(generate_quip, settings=self.settings, context=context_text)
        except Exception:
            logger.exception("generate_quip failed")
            return
        if quip:
            await bot.send_message(chat_id=chat_id, text=f"🤖 {quip}")

    # -- prediction poll -------------------------------------------------

    def _render_prediction_poll(self, tournament_id: int, players) -> tuple[str, InlineKeyboardMarkup]:
        rows: list[list[InlineKeyboardButton]] = []
        line: list[InlineKeyboardButton] = []
        for p in players:
            line.append(InlineKeyboardButton(p.first_name, callback_data=f"pr:{tournament_id}:{p.user_id}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        return "🔮 Кто заберёт вечер?", InlineKeyboardMarkup(rows)

    async def _handle_prediction_callback(self, query, parts: list[str]) -> None:
        tournament_id, user_id = int(parts[1]), int(parts[2])
        if self.storage.has_any_saved_match(tournament_id):
            await query.answer("Турнир уже начался, прогнозы закрыты.", show_alert=True)
            return
        self.storage.cast_prediction(
            tournament_id=tournament_id, voter_id=query.from_user.id, pick_user_id=user_id
        )
        await query.answer("Прогноз принят.")
        predictions = self.storage.get_predictions(tournament_id)
        voters = ", ".join(sorted({p["voter_name"] for p in predictions}))
        base_text = (query.message.text or "").split("\n\nПроголосовали")[0]
        await query.edit_message_text(
            f"{base_text}\n\nПроголосовали: {voters}", reply_markup=query.message.reply_markup
        )

    # -- MVP voting -----------------------------------------------------

    async def _handle_mvp_callback(self, query, parts: list[str]) -> None:
        tournament_id, user_id = int(parts[1]), int(parts[2])
        if query.from_user.id == user_id:
            await query.answer("За себя голосовать нельзя.", show_alert=True)
            return
        self.storage.cast_mvp_vote(
            tournament_id=tournament_id, voter_id=query.from_user.id, pick_user_id=user_id
        )
        await query.answer("Голос учтён.")
        tally = self.storage.get_mvp_tally(tournament_id)
        lines = ["🗳 MVP вечера"]
        if tally:
            top_votes = tally[0]["votes"]
            leaders = [t for t in tally if t["votes"] == top_votes]
            if len(leaders) == 1:
                word = "голос" if top_votes == 1 else ("голоса" if top_votes < 5 else "голосов")
                lines[0] = f"🗳 MVP вечера — {leaders[0]['first_name']} ({top_votes} {word})"
            lines.append(", ".join(f"{t['first_name']} {t['votes']}" for t in tally))
        await query.edit_message_text("\n".join(lines), reply_markup=query.message.reply_markup)

    # -- finish tournament -------------------------------------------------

    async def _finish_tournament_flow(self, query, tournament) -> None:
        tournament_id = tournament["tournament_id"]
        self.storage.finish_tournament(tournament_id)
        totals = self.storage.get_tournament_totals(tournament_id)
        ranked = sort_players(totals)
        game_label = GAME_LABELS.get(tournament["game"], tournament["game"])
        match_count = len(self.storage.list_recent_matches(tournament_id, limit=1000))

        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏁 Турнир «{tournament['name']}» завершён · {match_count} матчей", ""]
        for medal, p in zip(medals, ranked):
            lines.append(f"{medal} {p.first_name} — {p.tops} побед, {p.kills} убийств")
        await query.edit_message_text("\n".join(lines))

        table_text = format_table(tournament_name=tournament["name"], game_label=game_label, players=totals)
        await query.message.reply_text(table_text, parse_mode="HTML", do_quote=False)

        kill_pairs = self.storage.get_kill_matrix(tournament_id)
        matrix_text = format_kill_matrix(totals, kill_pairs)
        await query.message.reply_text(matrix_text, parse_mode="HTML", do_quote=False)

        titles = pick_titles(totals, kill_pairs)
        if titles:
            by_id = {p.user_id: p for p in totals}
            title_lines = ["🎖 Титулы вечера", ""]
            for uid, title, detail in titles:
                player = by_id.get(uid)
                if player:
                    title_lines.append(f"{title} — {player.first_name} — {detail}")
            await query.message.reply_text("\n".join(title_lines), do_quote=False)

        predictions = self.storage.get_predictions(tournament_id)
        if predictions and ranked:
            champion = ranked[0]
            hit = [p["voter_name"] for p in predictions if p["pick_user_id"] == champion.user_id]
            miss = [p["voter_name"] for p in predictions if p["pick_user_id"] != champion.user_id]
            pred_lines = ["🔮 Прогнозы"]
            if hit:
                pred_lines.append("✅ Угадали: " + ", ".join(hit))
            if miss:
                pred_lines.append("❌ Мимо: " + ", ".join(miss))
            await query.message.reply_text("\n".join(pred_lines), do_quote=False)

        if self.storage.get_setting(query.message.chat.id, "mvp") and len(totals) >= 2:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(p.first_name, callback_data=f"mv:{tournament_id}:{p.user_id}")] for p in ranked]
            )
            await query.message.reply_text("🗳 Голосуй за MVP вечера:", reply_markup=kb, do_quote=False)

        if self.settings.openai_api_key and totals:
            status = await query.message.reply_text("🤖 Считаю разбор от GPT...", do_quote=False)
            await self._run_analyze_for_tournament(tournament, totals, status)

    # -- GPT analysis -----------------------------------------------------

    async def _run_analyze(self, chat_id: int, status_message) -> None:
        tournament = self._resolve_view_target(chat_id)
        if tournament is None:
            await status_message.edit_text(
                "Пока нет турниров для разбора.", reply_markup=self._back_to_menu_kb()
            )
            return
        totals = self.storage.get_tournament_totals(tournament["tournament_id"])
        if not totals:
            await status_message.edit_text(
                "В этом турнире ещё нет сыгранных матчей.", reply_markup=self._back_to_menu_kb()
            )
            return
        await self._run_analyze_for_tournament(tournament, totals, status_message)

    async def _run_analyze_for_tournament(self, tournament, totals, status_message) -> None:
        tournament_id = tournament["tournament_id"]
        kill_pairs = self.storage.get_kill_matrix(tournament_id)
        kill_timing = self.storage.get_kill_timing_averages(tournament_id)
        team_by_user = {p["user_id"]: p["team"] for p in self.storage.get_draft_players(tournament_id)}
        try:
            analysis = await asyncio.to_thread(
                analyze_players,
                settings=self.settings,
                players=totals,
                kill_pairs=kill_pairs,
                kill_timing=kill_timing,
                team_by_user=team_by_user,
            )
        except SummaryError as exc:
            logger.error("analyze_players failed: %s", exc)
            await status_message.edit_text(exc.public_message, reply_markup=self._back_to_menu_kb())
            return
        text = self._format_analysis(tournament, totals, analysis, kill_timing)
        await status_message.edit_text(text, parse_mode="HTML", reply_markup=self._back_to_menu_kb())

    def _format_analysis(
        self, tournament, totals, analysis: dict[int, dict], kill_timing: dict[int, float]
    ) -> str:
        ranked = sort_players(totals)
        name_width = max([len(p.first_name) for p in ranked] + [5])
        table_lines = [f"{'Игрок':<{name_width}} {'Хладн.':>7} {'Жёстк.':>7} {'Интел.':>7} {'Живых':>7}"]
        for p in ranked:
            a = analysis.get(p.user_id)
            if not a:
                continue
            timing = kill_timing.get(p.user_id)
            timing_cell = f"{timing:.1f}" if timing is not None else "—"
            table_lines.append(
                f"{p.first_name:<{name_width}} {a['cool_headed']:>7} {a['brutality']:>7} "
                f"{a['game_iq']:>7} {timing_cell:>7}"
            )
        lines = [f"📊 Разбор турнира «{tournament['name']}»", "", "<pre>" + "\n".join(table_lines) + "</pre>", ""]
        lines.append("Выводы:")
        for p in ranked:
            a = analysis.get(p.user_id)
            if a and a.get("verdict"):
                lines.append(f"• {p.first_name} — {a['verdict']}")
        lines.append("")
        lines.append("90-100 Отлично · 75-89 Хорошо · 60-74 Средне · 40-59 Ниже среднего · 0-39 Плохо")
        lines.append(
            "Живых — среднее число живых соперников в момент килла: меньше = добивает последних, "
            "больше = лезет в замес рано"
        )
        return "\n".join(lines)

    # -- tournaments list --------------------------------------------------

    def _render_tournaments_screen(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        tournaments = self.storage.list_finished_tournaments(chat_id)
        if not tournaments:
            return "Пока нет завершённых турниров.", self._back_to_menu_kb()
        rows = [[InlineKeyboardButton(t["name"], callback_data=f"tl:{t['tournament_id']}")] for t in tournaments]
        rows.append([InlineKeyboardButton("← Меню", callback_data="mn:home")])
        return "🗂 Прошлые турниры", InlineKeyboardMarkup(rows)

    async def _handle_tournaments_callback(self, query, chat_id: int, parts: list[str]) -> None:
        if parts[1] == "back":
            await query.answer()
            text, kb = self._render_tournaments_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return
        tournament_id = int(parts[1])
        tournament = self.storage.get_tournament(tournament_id)
        if tournament is None:
            await query.answer("Турнир не найден.", show_alert=True)
            return
        totals = self.storage.get_tournament_totals(tournament_id)
        text = format_table(
            tournament_name=tournament["name"],
            game_label=GAME_LABELS.get(tournament["game"], tournament["game"]),
            players=totals,
        )
        await query.answer()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="tl:back")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    # -- players -------------------------------------------------------

    def _render_players_screen(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        players = self.storage.list_players(chat_id)
        real = [p for p in players if p.user_id > 0]
        guests = [p for p in players if p.user_id < 0]
        lines = ["👥 Игроки", ""]
        if real:
            lines.append("Из чата: " + ", ".join(p.first_name for p in real))
        if guests:
            lines.append("Добавлены вручную: " + ", ".join(p.first_name for p in guests))
        if not players:
            lines.append("Пока никого. Напишите в чат или добавьте вручную.")
        kb_rows = [[InlineKeyboardButton("➕ Добавить", callback_data="ap:menu")]]
        if players:
            kb_rows.append([InlineKeyboardButton("➖ Убрать", callback_data="pl:remove")])
        kb_rows.append([InlineKeyboardButton("← Меню", callback_data="mn:home")])
        return "\n".join(lines), InlineKeyboardMarkup(kb_rows)

    async def _handle_players_callback(self, query, chat_id: int, parts: list[str]) -> None:
        if parts[1] == "remove" and len(parts) == 2:
            players = self.storage.list_players(chat_id)
            if not players:
                await query.answer("Некого убирать.", show_alert=True)
                return
            rows = [[InlineKeyboardButton(p.first_name, callback_data=f"pl:remove:{p.user_id}")] for p in players]
            rows.append([InlineKeyboardButton("← Назад", callback_data="mn:players")])
            await query.answer()
            await query.edit_message_text("Кого убрать?", reply_markup=InlineKeyboardMarkup(rows))
            return
        if parts[1] == "remove" and len(parts) == 3:
            user_id = int(parts[2])
            self.storage.remove_player(chat_id=chat_id, user_id=user_id)
            await query.answer("Убран.")
            text, kb = self._render_players_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
            return

    async def _handle_add_player_callback(self, query, chat_id: int, parts: list[str]) -> None:
        origin = ":".join(parts[1:])
        await query.answer()
        prompt = await self._send_force_reply_prompt(
            query.message,
            query.from_user,
            "напиши имя нового игрока. Несколько — через запятую.",
            "Имя игрока",
        )
        self.storage.set_pending_input(
            chat_id=chat_id,
            user_id=query.from_user.id,
            kind=f"add_player:{origin}",
            prompt_message_id=prompt.message_id,
        )

    # -- settings ------------------------------------------------------

    def _render_settings_screen(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        rows = []
        for key, label in SETTINGS_LABELS.items():
            enabled = self.storage.get_setting(chat_id, key)
            rows.append(
                [InlineKeyboardButton(f"{label}: {'ВКЛ' if enabled else 'ВЫКЛ'}", callback_data=f"st:toggle:{key}")]
            )
        rows.append([InlineKeyboardButton("← Меню", callback_data="mn:home")])
        return "⚙️ Настройки", InlineKeyboardMarkup(rows)

    async def _handle_settings_callback(self, query, chat_id: int, parts: list[str]) -> None:
        if parts[1] == "toggle":
            self.storage.toggle_setting(chat_id, parts[2])
            await query.answer()
            text, kb = self._render_settings_screen(chat_id)
            await query.edit_message_text(text, reply_markup=kb)
