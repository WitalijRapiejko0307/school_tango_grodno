"""Morning gather (day RSVP) Telegram handlers."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from app.db import async_session_maker
from app.models import DayGather, DayGatherResponse
from app.services.billing import person_has_unpaid_started
from app.services.gather import (
    apply_gather_selection,
    find_pending_day_gather,
    local_school_date,
    parse_gather_indexes,
)

from .common import (
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _person_by_telegram,
)

router = Router(name="telegram.gather")

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())


@router.callback_query(F.data.startswith("gather_no:"))
async def cb_gather_no(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    day_gather_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала нажмите /start")
            return
        day_gather = await session.get(DayGather, day_gather_id)
        if day_gather is None or day_gather.person_id != person.id:
            await query.answer("Сообщение устарело")
            return
        if day_gather.response == DayGatherResponse.NONE:
            day_gather.response = DayGatherResponse.DECLINED
            await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(
            "Хорошо, тогда не ждём. До встречи в следующий раз."
        )


@router.message(F.text, F.chat.type == "private")
async def gather_numeric_reply(message: Message) -> None:
    if message.text is None or message.from_user is None:
        return
    indexes = parse_gather_indexes(message.text)
    if indexes is None:
        return
    now = datetime.now(UTC)
    gather_date = local_school_date(now)
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)
        if person is None:
            return
        day_gather = await find_pending_day_gather(session, person.id, gather_date)
        if day_gather is None:
            return
        ok, reply_text = await apply_gather_selection(
            session, day_gather, indexes, now
        )
        if ok:
            if await person_has_unpaid_started(session, person.id, gather_date):
                reply_text += (
                    "\n\nЕсть неоплаченный абонемент — его можно закрыть у администратора."
                )
        await session.commit()
    await message.answer(reply_text)
