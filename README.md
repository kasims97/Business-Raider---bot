# Chat Rating Bot

Telegram bot that tracks weekly chat activity in a single group and posts rankings every Sunday.

## Features

- Tracks messages, mentions, reactions received, reactions given
- Tracks forwards from channels/publics and video notes
- Commands:
  - `/топ` or `/top`
  - `/мойрейтинг` or `/myrating`
  - `/итоги` or `/summary`
- Weekly titles:
  - `🏆 Король чата`
  - `🤫 Молчун`
  - `🔥 Любимчик`
  - `💬 Самый активный`
  - `📰 Ньюсмейкер`
  - `🎥 Блогер`
  - `🪫 Беспонтовый`

## Environment

See `.env.example` for all required variables.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m bot.main
```

## Railway

- Create one service from this repo
- Attach a persistent volume and mount it to `/data`
- Set `DB_PATH=/data/bot.sqlite3`
- Set `BOT_TOKEN`, `CHAT_ID`, `TZ`, `POST_HOUR`, `POST_MINUTE`

The app uses long polling, so no webhook setup is required.
