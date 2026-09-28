# TangoGrodno

Backend for the TangoGrodno school bot and API (FastAPI, aiogram, SQLAlchemy async).

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` if you need non-default settings. For local development the default SQLite URL is enough.

## Run API

Set environment variables (see `.env.example`):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Async SQLAlchemy URL (default SQLite file) |
| `BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather); leave empty to run API/tests without a bot |
| `WEBHOOK_SECRET` | Optional secret for `X-Telegram-Bot-Api-Secret-Token` on webhook requests |
| `SCHOOL_TZ` | IANA timezone for schedule (default `Europe/Minsk`) |
| `PUBLIC_URL` | Public HTTPS base URL of this API (for Telegram webhook registration) |
| `BOOTSTRAP_ADMIN_TELEGRAM_ID` | Optional Telegram user id promoted to admin on first `/start` |

Telegram delivers updates to **`{PUBLIC_URL}/telegram/webhook`** (POST). Register that URL with BotFather when `BOT_TOKEN` is set.

```bash
uvicorn app.main:app --reload
```

Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

## Tests

```bash
pytest
```

## Deployment

The project owner deploys by pushing to git; this repository does not contain automated deploy configuration.
