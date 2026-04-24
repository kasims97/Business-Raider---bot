from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable


TITLE_KING = "🏆 Король чата"
TITLE_SILENT = "🤫 Молчун"
TITLE_FAVORITE = "🔥 Любимчик"
TITLE_ACTIVE = "💬 Самый активный"
TITLE_NEWSMAKER = "📰 Ньюсмейкер"
TITLE_BLOGGER = "🎥 Блогер"
TITLE_USELESS = "🪫 Беспонтовый"


@dataclass(slots=True)
class UserWeekStats:
    user_id: int
    username: str | None
    first_name: str
    messages: int = 0
    reactions_received: int = 0
    reactions_given: int = 0
    mentions: int = 0
    forwards_public: int = 0
    video_notes: int = 0
    rep_plus: int = 0
    rep_minus: int = 0

    @property
    def score(self) -> float:
        return compute_score(
            messages=self.messages,
            reactions_received=self.reactions_received,
            reactions_given=self.reactions_given,
            mentions=self.mentions,
            rep_balance=self.rep_balance,
        )

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name

    @property
    def rep_balance(self) -> int:
        return self.rep_plus - self.rep_minus


def compute_score(
    *,
    messages: int,
    reactions_received: int,
    reactions_given: int,
    mentions: int,
    rep_balance: int = 0,
) -> float:
    return (
        messages * 1
        + reactions_received * 3
        + mentions * 2
        + reactions_given * 0.5
        + rep_balance * 2
    )


def week_start_for_day(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_start_for_dt(moment: datetime) -> date:
    return week_start_for_day(moment.date())


def week_key(week_start: date) -> str:
    return week_start.isoformat()


def sort_for_ranking(items: Iterable[UserWeekStats]) -> list[UserWeekStats]:
    return sorted(
        items,
        key=lambda item: (-item.score, -item.messages, item.user_id),
    )


def pick_titles(items: Iterable[UserWeekStats]) -> list[tuple[int, str]]:
    stats = list(items)
    if not stats:
        return []

    def best(metric: str, reverse: bool = True) -> UserWeekStats:
        return sorted(
            stats,
            key=lambda item: (
                getattr(item, metric),
                item.score,
                item.messages,
                -item.user_id,
            ),
            reverse=reverse,
        )[0]

    def worst(metric: str) -> UserWeekStats:
        return sorted(
            stats,
            key=lambda item: (
                getattr(item, metric),
                item.score,
                item.messages,
                item.user_id,
            ),
        )[0]

    return [
        (best("score").user_id, TITLE_KING),
        (worst("messages").user_id, TITLE_SILENT),
        (best("reactions_received").user_id, TITLE_FAVORITE),
        (best("messages").user_id, TITLE_ACTIVE),
        (best("forwards_public").user_id, TITLE_NEWSMAKER),
        (best("video_notes").user_id, TITLE_BLOGGER),
        (worst("score").user_id, TITLE_USELESS),
    ]


def format_score(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def format_ranking(stats: list[UserWeekStats], title: str) -> str:
    if not stats:
        return f"{title}\n\nПока тихо. Как только в чате начнётся движ, здесь появится рейтинг."

    lines = [title, ""]
    for idx, item in enumerate(stats, start=1):
        lines.append(
            f"{idx}. {item.display_name} — {format_score(item.score)} очков"
        )
        lines.append(
            f"{item.messages} сообщений • {item.reactions_received} реакций • "
            f"{item.mentions} упоминаний • rep {item.rep_balance:+d}"
        )
    return "\n".join(lines)


def format_personal_stats(
    stats: UserWeekStats,
    rank: int,
    total: int,
    titles: list[str],
) -> str:
    lines = [
        f"Твоё место: {rank} из {total}",
        f"Очки за неделю: {format_score(stats.score)}",
        "",
        f"Сообщения: {stats.messages}",
        f"Реакции получил: {stats.reactions_received}",
        f"Реакции поставил: {stats.reactions_given}",
        f"Упоминания: {stats.mentions}",
        f"Пересылки из пабликов: {stats.forwards_public}",
        f"Кружочки: {stats.video_notes}",
        f"Репутация: +{stats.rep_plus} / -{stats.rep_minus} (итог {stats.rep_balance:+d})",
    ]
    if titles:
        lines.extend(["", "Титулы этой недели:"] + titles)
    return "\n".join(lines)


def format_weekly_summary(
    week_start: date,
    ranked: list[UserWeekStats],
    titles: list[tuple[UserWeekStats, str]],
    *,
    official: bool,
) -> str:
    week_end = week_start + timedelta(days=6)
    lines = [
        (
            f"Итоги недели {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')}"
            if official
            else f"Промежуточные итоги недели {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')}"
        ),
        "",
        "Рейтинг:",
    ]
    if ranked:
        for idx, item in enumerate(ranked, start=1):
            lines.append(f"{idx}. {item.display_name} — {format_score(item.score)} очков")
    else:
        lines.append("Пока тихо. На этой неделе рейтинг ещё не собрался.")

    lines.extend(["", "Титулы:"])
    if titles:
        for item, title in titles:
            lines.append(f"{title} — {item.display_name}")
    else:
        lines.append("На этой неделе пока без титулов.")
    return "\n".join(lines)
