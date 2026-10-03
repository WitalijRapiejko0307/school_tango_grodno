from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.channels.tracking_bot import TrackingBot


async def test_answer_path_is_tracked() -> None:
    bot = TrackingBot(token="1:test")
    method = SendMessage(
        chat_id=7,
        text="hello",
        reply_markup=ReplyKeyboardRemove(),
    )
    sent = SimpleNamespace(message_id=9)
    with (
        patch.object(Bot, "__call__", new_callable=AsyncMock, return_value=sent),
        patch(
            "app.channels.tracking_bot.track_sent_message", new_callable=AsyncMock
        ) as track,
    ):
        result = await bot(method)

    assert result is sent
    track.assert_awaited_once_with(
        bot,
        7,
        9,
        reply_markup=method.reply_markup,
    )


async def test_delete_is_not_tracked() -> None:
    bot = TrackingBot(token="1:test")
    method = DeleteMessage(chat_id=7, message_id=9)
    with (
        patch.object(Bot, "__call__", new_callable=AsyncMock, return_value=True),
        patch(
            "app.channels.tracking_bot.track_sent_message", new_callable=AsyncMock
        ) as track,
    ):
        result = await bot(method)

    assert result is True
    track.assert_not_awaited()


async def test_reply_keyboard_is_passed_through() -> None:
    bot = TrackingBot(token="1:test")
    markup = ReplyKeyboardMarkup(keyboard=[[{"text": "Прайс"}]])
    method = SendMessage(chat_id=7, text="menu", reply_markup=markup)
    with (
        patch.object(
            Bot,
            "__call__",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(message_id=3),
        ),
        patch(
            "app.channels.tracking_bot.track_sent_message", new_callable=AsyncMock
        ) as track,
    ):
        await bot(method)

    assert track.await_args.kwargs["reply_markup"] is markup
