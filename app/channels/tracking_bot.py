"""Bot subclass that records message ids for chat cleanup."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.methods import SendMessage, TelegramMethod

from app.services.bot_messages import track_sent_message

logger = logging.getLogger(__name__)


class TrackingBot(Bot):
    async def __call__(
        self, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        result = await super().__call__(method, request_timeout=request_timeout)
        if not isinstance(method, SendMessage):
            return result
        chat_id = method.chat_id
        message_id = getattr(result, "message_id", None)
        if not isinstance(chat_id, int) or message_id is None:
            return result
        try:
            await track_sent_message(
                self,
                chat_id,
                message_id,
                reply_markup=method.reply_markup,
            )
        except Exception:
            logger.exception(
                "Failed to track bot message %s in chat %s", message_id, chat_id
            )
        return result
