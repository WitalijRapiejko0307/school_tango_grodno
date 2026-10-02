"""Track outbound Telegram bot messages and apply chat cleanup rules."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import date, datetime

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import BotChatMessage, Person, SchoolSession
from app.services.school_time import school_tz

logger = logging.getLogger(__name__)

_outbound_school_session_id: ContextVar[str | None] = ContextVar(
    "outbound_school_session_id", default=None
)

ADMIN_CHAT_LIMIT = 20
CLIENT_GUEST_CHAT_LIMIT = 10


def outbound_school_session_id() -> str | None:
    return _outbound_school_session_id.get()


def bind_outbound_school_session_id(session_id: str | None):
    return _outbound_school_session_id.set(session_id)


def reset_outbound_context(token) -> None:
    _outbound_school_session_id.reset(token)


def _chat_limit(role: str) -> int:
    return ADMIN_CHAT_LIMIT if role == "admin" else CLIENT_GUEST_CHAT_LIMIT


def _reply_markup_sets_menu(reply_markup: object | None) -> bool:
    return isinstance(reply_markup, ReplyKeyboardMarkup)


def _session_local_date(session_row: SchoolSession) -> date:
    if session_row.session_date is not None:
        return session_row.session_date
    starts = session_row.starts_at
    if starts.tzinfo is None:
        return starts.date()
    return starts.astimezone(school_tz()).date()


async def _try_delete_message(
    session: AsyncSession, bot: Bot, row: BotChatMessage
) -> None:
    try:
        await bot.delete_message(row.chat_id, row.telegram_message_id)
    except Exception:
        logger.exception(
            "Failed to delete bot message %s in chat %s",
            row.telegram_message_id,
            row.chat_id,
        )
        return
    await session.delete(row)


async def apply_message_deletions(
    session: AsyncSession,
    bot: Bot,
    person: Person,
    *,
    today: date | None = None,
) -> None:
    if today is None:
        today = datetime.now(school_tz()).date()

    result = await session.execute(
        select(BotChatMessage)
        .where(BotChatMessage.person_id == person.id)
        .order_by(BotChatMessage.sent_at.asc())
    )
    rows = list(result.scalars().all())
    if not rows:
        return

    session_ids = {r.school_session_id for r in rows if r.school_session_id}
    sessions_by_id: dict[str, SchoolSession] = {}
    if session_ids:
        sess_result = await session.execute(
            select(SchoolSession).where(SchoolSession.id.in_(session_ids))
        )
        sessions_by_id = {s.id: s for s in sess_result.scalars()}

    for row in list(rows):
        sid = row.school_session_id
        if not sid:
            continue
        school_session = sessions_by_id.get(sid)
        if school_session is None:
            continue
        if _session_local_date(school_session) < today:
            await _try_delete_message(session, bot, row)

    result = await session.execute(
        select(BotChatMessage)
        .where(BotChatMessage.person_id == person.id)
        .order_by(BotChatMessage.sent_at.asc())
    )
    rows = list(result.scalars().all())
    limit = _chat_limit(person.role)
    non_anchor = [r for r in rows if not r.is_reply_menu_anchor]
    while len(non_anchor) > limit:
        oldest = non_anchor.pop(0)
        await _try_delete_message(session, bot, oldest)


async def record_bot_message(
    session: AsyncSession,
    bot: Bot,
    *,
    person: Person,
    chat_id: int,
    telegram_message_id: int,
    school_session_id: str | None = None,
    sets_reply_menu: bool = False,
    sent_at: datetime | None = None,
) -> None:
    if sets_reply_menu:
        await session.execute(
            update(BotChatMessage)
            .where(BotChatMessage.person_id == person.id)
            .values(is_reply_menu_anchor=False)
        )
    row = BotChatMessage(
        person_id=person.id,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        school_session_id=school_session_id,
        is_reply_menu_anchor=sets_reply_menu,
        sent_at=sent_at or datetime.now(school_tz()),
    )
    session.add(row)
    await session.flush()
    await apply_message_deletions(session, bot, person)


async def track_sent_message(
    bot: Bot,
    chat_id: int,
    telegram_message_id: int,
    *,
    reply_markup: object | None = None,
    school_session_id: str | None = None,
) -> None:
    sets_menu = _reply_markup_sets_menu(reply_markup)
    sid = school_session_id if school_session_id is not None else outbound_school_session_id()
    async with async_session_maker() as session:
        result = await session.execute(
            select(Person).where(Person.telegram_user_id == chat_id)
        )
        person = result.scalar_one_or_none()
        if person is None:
            return
        await record_bot_message(
            session,
            bot,
            person=person,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            school_session_id=sid,
            sets_reply_menu=sets_menu,
        )
        await session.commit()
