import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.channels.telegram import reminder_inline_markup, router as telegram_router
from app.config import get_settings
from app.db import Base, async_session_maker, engine
from app.services.notifications import mark_sent, plan_due

logger = logging.getLogger(__name__)

_storage = MemoryStorage()
_dispatcher = Dispatcher(storage=_storage)
_dispatcher.include_router(telegram_router)


async def _reminder_loop(bot: Bot) -> None:
    while True:
        try:
            async with async_session_maker() as session:
                now = datetime.now(UTC)
                due_list = await plan_due(session, now)
                for item in due_list:
                    if item.telegram_user_id is None:
                        continue
                    markup = reminder_inline_markup(item)
                    try:
                        await bot.send_message(
                            item.telegram_user_id,
                            item.text,
                            reply_markup=markup,
                        )
                        await mark_sent(
                            session,
                            item.reminder_id,
                            now,
                            nudge=item.kind == "attendance_nudge",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send reminder %s to %s",
                            item.reminder_id,
                            item.telegram_user_id,
                        )
                await session.commit()
        except Exception:
            logger.exception("Reminder loop iteration failed")
        await asyncio.sleep(60)


async def _register_telegram_webhook(bot: Bot, settings) -> None:
    public_url = settings.PUBLIC_URL.strip()
    if not public_url:
        return
    webhook_url = f"{public_url.rstrip('/')}/telegram/webhook"
    kwargs: dict[str, str] = {}
    if settings.WEBHOOK_SECRET:
        kwargs["secret_token"] = settings.WEBHOOK_SECRET
    await bot.set_webhook(webhook_url, **kwargs)
    logger.info("Telegram webhook registered for %s", webhook_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    bot: Bot | None = None
    task: asyncio.Task | None = None
    if settings.BOT_TOKEN:
        bot = Bot(token=settings.BOT_TOKEN)
        if settings.PUBLIC_URL.strip():
            try:
                await _register_telegram_webhook(bot, settings)
            except Exception:
                logger.exception("Failed to register Telegram webhook")
        task = asyncio.create_task(_reminder_loop(bot))

    app.state.bot = bot
    app.state.dispatcher = _dispatcher

    yield

    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if bot is not None:
        await bot.session.close()


app = FastAPI(title="TangoGrodno", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")

    settings = get_settings()
    if settings.WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != settings.WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    dispatcher: Dispatcher = request.app.state.dispatcher
    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    await dispatcher.feed_update(bot, update)
    return {"ok": True}
