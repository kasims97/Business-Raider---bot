from __future__ import annotations

import json
from urllib import error, request

from bot.config import Settings
from bot.stats import PlayerTotals


class SummaryError(RuntimeError):
    """Raised when an OpenAI request fails."""

    def __init__(self, message: str, *, public_message: str | None = None):
        super().__init__(message)
        self.public_message = public_message or (
            "Не получилось получить ответ от OpenAI прямо сейчас. Попробуй ещё раз чуть позже."
        )


def _call_responses_api(payload: dict, *, api_key: str) -> dict:
    req = request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SummaryError(
            f"OpenAI API error {exc.code}: {body}",
            public_message=_public_message_for_http_error(exc.code, body),
        ) from exc
    except error.URLError as exc:
        raise SummaryError(
            f"OpenAI API is unavailable right now: {exc}",
            public_message="OpenAI сейчас не отвечает. Попробуй ещё раз чуть позже.",
        ) from exc


def _extract_output_text(data: dict) -> str:
    text = data.get("output_text")
    if text:
        return text.strip()
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks).strip()
    raise SummaryError("OpenAI returned an empty response")


def analyze_players(
    *,
    settings: Settings,
    players: list[PlayerTotals],
    kill_pairs: dict[tuple[int, int], int],
) -> dict[int, dict]:
    """Returns {user_id: {cool_headed, brutality, game_iq, verdict}}."""
    if not settings.openai_api_key:
        raise SummaryError(
            "OPENAI_API_KEY is not configured",
            public_message="Для разбора не задан OPENAI_API_KEY. Добавь ключ в Railway Variables.",
        )
    if not players:
        raise SummaryError("No players to analyze")

    stats_lines = []
    for p in players:
        stats_lines.append(
            f"user_id={p.user_id} имя={p.first_name} матчей={p.played} побед={p.tops} "
            f"убийств={p.kills} смертей={p.deaths} KD={p.kd:.2f}"
        )
    duels = [
        f"{killer}→{victim}: {count}" for (killer, victim), count in sorted(
            kill_pairs.items(), key=lambda item: -item[1]
        )[:10]
    ]

    intro = (
        "Ты спортивный аналитик киберспорта. По сухой статистике турнира PUBG/CS оцени каждого "
        "игрока по трём шкалам от 0 до 100: хладнокровие (cool_headed, стабильность и спокойствие), "
        "жестокость (brutality, агрессия и давление на соперников), игровой интеллект (game_iq, "
        "принятие решений и стратегия). Оценивай игроков относительно друг друга внутри этого турнира. "
        "Для каждого напиши короткий вывод на русском (verdict, одна-две фразы, разговорный тон, "
        "как тренерский разбор). Обязательно верни ровно один объект на каждый переданный user_id."
    )
    user_text = "Статистика игроков:\n" + "\n".join(stats_lines)
    if duels:
        user_text += "\n\nКто кого чаще всего убивал (killer_id→victim_id: раз):\n" + "\n".join(duels)

    payload = {
        "model": settings.openai_summary_model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": intro}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "player_analysis",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "players": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "user_id": {"type": "integer"},
                                    "cool_headed": {"type": "integer"},
                                    "brutality": {"type": "integer"},
                                    "game_iq": {"type": "integer"},
                                    "verdict": {"type": "string"},
                                },
                                "required": ["user_id", "cool_headed", "brutality", "game_iq", "verdict"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["players"],
                    "additionalProperties": False,
                },
            }
        },
    }

    data = _call_responses_api(payload, api_key=settings.openai_api_key)
    text = _extract_output_text(data)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SummaryError(f"OpenAI returned invalid JSON: {text[:200]}") from exc

    return {int(item["user_id"]): item for item in parsed.get("players", [])}


def generate_quip(*, settings: Settings, context: str) -> str | None:
    """Fire-and-forget one-liner about what just happened. Returns None on any failure."""
    if not settings.openai_api_key:
        return None
    payload = {
        "model": settings.openai_summary_model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Ты токсичный, но добрый комментатор дружеского игрового турнира. "
                            "По короткому описанию только что сыгранного матча напиши ОДНУ едкую, "
                            "смешную фразу на русском языке про то, что произошло. Без вступлений, "
                            "без кавычек, максимум 20 слов."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "input_text", "text": context}]},
        ],
    }
    try:
        data = _call_responses_api(payload, api_key=settings.openai_api_key)
        return _extract_output_text(data)
    except SummaryError:
        return None


def _public_message_for_http_error(status_code: int, body: str) -> str:
    body_lower = body.lower()
    if status_code == 401:
        return "OpenAI ключ не прошёл проверку. Проверь OPENAI_API_KEY в Railway Variables."
    if status_code == 403:
        return "У OpenAI ключа нет доступа к выбранной модели. Проверь OPENAI_SUMMARY_MODEL."
    if status_code == 404 or "model" in body_lower:
        return "OpenAI не нашёл выбранную модель. Проверь OPENAI_SUMMARY_MODEL в Railway Variables."
    if status_code == 429:
        if any(marker in body_lower for marker in ("quota", "billing", "insufficient")):
            return "Похоже, закончился баланс или лимит OpenAI. Проверь billing в OpenAI."
        return "OpenAI временно ограничил запросы. Попробуй чуть позже."
    if status_code >= 500:
        return "На стороне OpenAI временная ошибка. Попробуй чуть позже."
    return "OpenAI вернул ошибку. Подробности есть в Railway logs."
