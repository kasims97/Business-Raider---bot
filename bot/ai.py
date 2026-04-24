from __future__ import annotations

import json
from urllib import error, request

from bot.config import Settings


class SummaryError(RuntimeError):
    """Raised when the summary request fails."""


def summarize_chat(
    *,
    settings: Settings,
    transcript_blocks: list[str],
    missing_audio_count: int,
) -> str:
    if not settings.openai_api_key:
        raise SummaryError("OPENAI_API_KEY is not configured")
    if not transcript_blocks:
        raise SummaryError("No content available for summarization")

    intro = (
        "Сделай краткое резюме последних сообщений чата на русском языке. "
        "Пиши живо и понятно, без канцелярита. "
        "Структура ответа: заголовок, блок 'Если кратко', блок 'Что обсуждали', "
        "и блок 'К чему пришли', если есть явные выводы."
    )
    if missing_audio_count:
        intro += (
            " В конце коротко добавь заметку, что часть голосовых или кружочков "
            "не вошла в резюме, потому что ещё без расшифровки."
        )

    payload = {
        "model": settings.openai_summary_model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": intro}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "\n\n".join(transcript_blocks),
                    }
                ],
            },
        ],
    }
    req = request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SummaryError(f"OpenAI API error: {body}") from exc
    except error.URLError as exc:
        raise SummaryError("OpenAI API is unavailable right now") from exc

    text = data.get("output_text")
    if text:
        return text.strip()

    output_chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                output_chunks.append(content["text"])
    if output_chunks:
        return "\n".join(output_chunks).strip()

    raise SummaryError("OpenAI returned an empty summary")
