"""Bot subclass that records message ids for chat cleanup."""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import Message

from app.services.bot_messages import track_sent_message


class TrackingBot(Bot):
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        **kwargs: Any,
    ) -> Message:
        msg = await super().send_message(chat_id, text, **kwargs)
        if isinstance(chat_id, int) and msg.message_id is not None:
            await track_sent_message(
                self,
                chat_id,
                msg.message_id,
                reply_markup=kwargs.get("reply_markup"),
            )
        return msg
