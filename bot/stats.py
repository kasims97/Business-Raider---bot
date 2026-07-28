from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable


LEAGUE_POINTS_PER_TOP = 3
LEAGUE_POINTS_PER_KILL = 1

LEAGUE_RANKS = [
    (150, "🥉 Бронза"),
    (350, "🥈 Серебро"),
    (600, "🥇 Золото"),
]
LEAGUE_TOP_RANK = "👑 Легенда"

TITLE_CHAMPION = "🥇 ЧЕМПИОН"
TITLE_BUTCHER = "🔪 МЯСНИК"
TITLE_SNIPER = "🎯 СНАЙПЕР"
TITLE_TANK = "🧱 ТАНК"
TITLE_CLOWN = "🎪 КЛОУН"
TITLE_CHICKEN = "🐔 КУРИЦА"
TITLE_NEMESIS = "😤 НЕМЕЗИДА ВЕЧЕРА"


@dataclass(slots=True)
class PlayerTotals:
    user_id: int
    username: str | None
    first_name: str
    played: int = 0
    kills: int = 0
    tops: int = 0

    @property
    def deaths(self) -> int:
        return max(self.played - self.tops, 0)

    @property
    def kd(self) -> float:
        deaths = self.deaths
        return self.kills / deaths if deaths else float(self.kills)

    @property
    def display_name(self) -> str:
        if self.username:
            return f"{self.first_name} (t.me/{self.username})"
        return self.first_name

    @property
    def league_points(self) -> int:
        return self.tops * LEAGUE_POINTS_PER_TOP + self.kills * LEAGUE_POINTS_PER_KILL

    @property
    def kills_per_match(self) -> float:
        return self.kills / self.played if self.played else 0.0


def sort_players(players: Iterable[PlayerTotals]) -> list[PlayerTotals]:
    return sorted(
        players,
        key=lambda p: (-p.tops, -p.kd, -p.kills, p.user_id),
    )


def sort_by_league_points(players: Iterable[PlayerTotals]) -> list[PlayerTotals]:
    return sorted(players, key=lambda p: (-p.league_points, -p.tops, p.user_id))


def league_rank(points: int) -> str:
    for threshold, label in LEAGUE_RANKS:
        if points < threshold:
            return label
    return LEAGUE_TOP_RANK


def format_kd(value: float) -> str:
    return f"{value:.2f}"


def format_table(*, tournament_name: str, game_label: str, players: list[PlayerTotals]) -> str:
    if not players:
        return f"🏆 {tournament_name} · {game_label}\n\nПока ни одного сыгранного матча."

    ranked = sort_players(players)
    total_matches = max((p.played for p in ranked), default=0)
    total_kills = sum(p.kills for p in ranked)
    total_tops = sum(p.tops for p in ranked)
    total_deaths = sum(p.deaths for p in ranked)

    by_tops = sorted(ranked, key=lambda p: (-p.tops, -p.kills, p.user_id))[:5]
    by_kills = sorted(ranked, key=lambda p: (-p.kills, -p.tops, p.user_id))[:5]

    left = ["Победы"] + [f"{i}. {p.first_name} — {p.tops}" for i, p in enumerate(by_tops, start=1)]
    right = ["Убийства"] + [f"{i}. {p.first_name} — {p.kills}" for i, p in enumerate(by_kills, start=1)]
    width = max(len(x) for x in left) + 3
    summary_lines = [f"{l.ljust(width)}{r}" for l, r in zip(left, right)]

    name_width = max([len(p.first_name) for p in ranked] + [5])
    header = f"{'#':<2} {'Игрок':<{name_width}} {'М':>3} {'K':>4} {'D':>4} {'KD':>5} {'К/М':>5} {'Топы':>10}"
    table_lines = [header]
    for idx, p in enumerate(ranked, start=1):
        pct = f" ({p.tops * 100 // total_tops}%)" if total_tops else ""
        top_cell = f"{p.tops}{pct}"
        table_lines.append(
            f"{idx:<2} {p.first_name:<{name_width}} {p.played:>3} {p.kills:>4} "
            f"{p.deaths:>4} {format_kd(p.kd):>5} {p.kills_per_match:>5.1f} {top_cell:>10}"
        )
    total_kd = total_kills / total_deaths if total_deaths else float(total_kills)
    total_kills_per_match = total_kills / total_matches if total_matches else 0.0
    table_lines.append(
        f"{'Σ':<2} {'ВСЕГО':<{name_width}} {total_matches:>3} {total_kills:>4} "
        f"{total_deaths:>4} {format_kd(total_kd):>5} {total_kills_per_match:>5.1f} {total_tops:>10}"
    )

    body = "\n\n".join(["\n".join(summary_lines), "\n".join(table_lines)])
    return f"🏆 {tournament_name} · {game_label} · матчей: {total_matches}\n\n<pre>{body}</pre>"


MONTH_NAMES_RU_GENITIVE = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def format_monthly_recap(
    *, month: int, players: list[PlayerTotals], tournament_count: int, match_count: int
) -> str | None:
    if not players:
        return None
    ranked = sort_by_league_points(players)
    lines = [f"📅 Итоги {MONTH_NAMES_RU_GENITIVE[month]} · {tournament_count} турниров · {match_count} матчей", ""]
    lines.append(f"🏅 Игрок месяца: {ranked[0].first_name}")
    lines.append("")
    for i, p in enumerate(ranked, start=1):
        rank = league_rank(p.league_points)
        lines.append(
            f"{i}. {rank} {p.first_name}   {p.league_points} · {p.tops} побед · "
            f"{p.kills} убийств · {p.kills_per_match:.1f} уб/матч · KD {format_kd(p.kd)}"
        )
    return "\n".join(lines)


def format_kill_matrix(players: list[PlayerTotals], kill_pairs: dict[tuple[int, int], int]) -> str:
    if not kill_pairs:
        return "⚔️ Кто кого убивал\n\nПока рандомные фраги, никто ещё не запомнился."

    ranked = sort_players(players)
    by_id = {p.user_id: p for p in ranked}
    name_width = max(4, max((len(p.first_name[:6]) for p in ranked), default=4))

    header = " " * (name_width + 1) + " ".join(p.first_name[:6].rjust(name_width) for p in ranked)
    matrix_lines = [header]
    for killer in ranked:
        row = [killer.first_name[:6].ljust(name_width)]
        for victim in ranked:
            if killer.user_id == victim.user_id:
                row.append("·".rjust(name_width))
            else:
                count = kill_pairs.get((killer.user_id, victim.user_id), 0)
                row.append((str(count) if count else "·").rjust(name_width))
        matrix_lines.append(" ".join(row))

    lines = ["⚔️ Кто кого убивал", "", f"<pre>{chr(10).join(matrix_lines)}</pre>"]

    best_pair = max(kill_pairs.items(), key=lambda item: item[1], default=None)
    if best_pair and best_pair[1] >= 2:
        (killer_id, victim_id), count = best_pair
        killer = by_id.get(killer_id)
        victim = by_id.get(victim_id)
        if killer and victim:
            lines.append("")
            lines.append(f"🔪 Любимая жертва: {victim.first_name} у {killer.first_name} ({count} раз)")

    return "\n".join(lines)


def pick_titles(
    players: list[PlayerTotals],
    kill_pairs: dict[tuple[int, int], int],
) -> list[tuple[int, str, str]]:
    if not players:
        return []

    titles: list[tuple[int, str, str]] = []
    ranked = sort_players(players)

    champion = ranked[0]
    if champion.tops > 0:
        titles.append((champion.user_id, TITLE_CHAMPION, f"{champion.tops} побед"))

    butcher = max(players, key=lambda p: (p.kills, -p.user_id))
    if butcher.kills > 0:
        titles.append((butcher.user_id, TITLE_BUTCHER, f"{butcher.kills} убийств"))

    with_deaths = [p for p in players if p.deaths > 0]
    if with_deaths:
        sniper = max(with_deaths, key=lambda p: (p.kd, -p.user_id))
        if sniper.kd > 1:
            titles.append((sniper.user_id, TITLE_SNIPER, f"KD {format_kd(sniper.kd)}"))

    tankable = [p for p in players if p.tops > 0]
    if tankable:
        tank = max(tankable, key=lambda p: (p.tops / max(p.kills, 1), -p.user_id))
        if tank.kills <= tank.tops:
            titles.append(
                (tank.user_id, TITLE_TANK, f"{tank.tops} топов при {tank.kills} убийствах")
            )

    if with_deaths:
        clown = min(with_deaths, key=lambda p: (p.kd, p.user_id))
        if clown.kd < 0.5:
            titles.append(
                (clown.user_id, TITLE_CLOWN, f"умер {clown.deaths} раз из {clown.played}")
            )

    no_tops = [p for p in players if p.tops == 0 and p.played > 0]
    if no_tops:
        chicken = max(no_tops, key=lambda p: (p.played, -p.user_id))
        titles.append((chicken.user_id, TITLE_CHICKEN, "ни одного топа за вечер"))

    if kill_pairs:
        (killer_id, victim_id), count = max(kill_pairs.items(), key=lambda item: item[1])
        if count >= 2:
            victim = next((p for p in players if p.user_id == victim_id), None)
            if victim:
                titles.append(
                    (killer_id, TITLE_NEMESIS, f"→ {victim.first_name}, вынес {count} раз")
                )

    return titles


def compute_current_streaks(
    match_results: list[dict[int, bool]],
) -> dict[int, tuple[str, int]]:
    """match_results: chronological list of {user_id: won_this_match}."""
    per_user: dict[int, list[bool]] = {}
    for match in match_results:
        for user_id, won in match.items():
            per_user.setdefault(user_id, []).append(won)

    streaks: dict[int, tuple[str, int]] = {}
    for user_id, results in per_user.items():
        if not results:
            continue
        last = results[-1]
        length = 0
        for won in reversed(results):
            if won != last:
                break
            length += 1
        if length >= 2:
            label = "🔥" if last else "💀"
            streaks[user_id] = (label, length)
    return streaks


def best_match_kills(kills_per_match: dict[int, dict[int, int]]) -> dict[int, tuple[int, int]]:
    """kills_per_match: {match_id: {user_id: kills_in_that_match}}. Returns {user_id: (match_id, kills)}."""
    best: dict[int, tuple[int, int]] = {}
    for match_id, per_user in kills_per_match.items():
        for user_id, kills in per_user.items():
            current = best.get(user_id)
            if current is None or kills > current[1]:
                best[user_id] = (match_id, kills)
    return best


def merge_totals(players: Iterable[PlayerTotals]) -> list[PlayerTotals]:
    merged: dict[int, PlayerTotals] = {}
    for p in players:
        if p.user_id in merged:
            existing = merged[p.user_id]
            existing.played += p.played
            existing.kills += p.kills
            existing.tops += p.tops
        else:
            merged[p.user_id] = replace(p)
    return list(merged.values())
