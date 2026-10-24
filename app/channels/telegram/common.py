"""Shared Telegram bot helpers (keyboards, auth, notices, reminders)."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import async_session_maker
from app.models import Person
from app.services.bot_messages import track_user_message
from app.services.identity import upsert_from_telegram
from app.services.notices import countable, delivery_report
from app.services.notifications import DueReminder
from app.services.pick_list import (
    HINT_INDEX,
    HINT_MANY,
    PickRow,
    has_next_page,
    page_text,
)
from app.services.schedule import WEEKDAY_LABELS

from .states import ListPickFSM

logger = logging.getLogger(__name__)

ARCHIVED_ACCESS_CLOSED = "Доступ закрыт."

# --- Menu labels (variant A) ---

BTN_TODAY = "Сегодня"
BTN_GROUPS = "Группы"
BTN_PEOPLE = "Пользователи"
BTN_PRICE = "Прайс"
BTN_CLIENT_SUBS = "Абонементы"
BTN_CLIENT_CLASSES = "Занятия"
BTN_REMINDERS = "Напоминания"
BTN_NOTICE = "Сообщение"
BTN_MY_GROUP = "Моя группа"
BTN_OTHER_GROUP = "Не со своей группой"
BTN_BALANCE = "Остаток"
BTN_SCHEDULE = "Расписание"
BTN_CONTACTS = "Контакты"

ADMIN_MENU = [
    BTN_TODAY,
    BTN_GROUPS,
    BTN_PEOPLE,
    BTN_PRICE,
    BTN_REMINDERS,
    BTN_NOTICE,
]
CLIENT_MENU = [BTN_PRICE, BTN_CLIENT_SUBS, BTN_CLIENT_CLASSES]
GUEST_MENU = [BTN_SCHEDULE, BTN_CONTACTS]

REFUSAL = "Эта команда доступна только администраторам."

BTN_SHARE_PHONE = "Подтвердить телефон"


class _TrackUserMessageMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        finally:
            if (
                isinstance(event, Message)
                and event.from_user is not None
                and not event.from_user.is_bot
                and event.bot is not None
            ):
                try:
                    await track_user_message(
                        event.bot, event.chat.id, event.message_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to track user message %s in chat %s",
                        event.message_id,
                        event.chat.id,
                    )


def role_keyboard(role: str) -> list[str]:
    """Flat list of reply-keyboard button texts for the role."""
    if role == "admin":
        return list(ADMIN_MENU)
    if role == "client":
        return list(CLIENT_MENU)
    return list(GUEST_MENU)


def _guest_keyboard_with_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SHARE_PHONE, request_contact=True)],
            [KeyboardButton(text=BTN_SCHEDULE), KeyboardButton(text=BTN_CONTACTS)],
        ],
        resize_keyboard=True,
    )


def reply_markup_for_role(role: str) -> ReplyKeyboardMarkup:
    texts = role_keyboard(role)
    if role in ("admin", "client"):
        rows = [
            [KeyboardButton(text=t) for t in texts[i : i + 2]]
            for i in range(0, len(texts), 2)
        ]
    else:
        rows = [[KeyboardButton(text=t) for t in texts]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _weekday_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=WEEKDAY_LABELS[i], callback_data=f"{prefix}:{i}")
                for i in range(0, 4)
            ],
            [
                InlineKeyboardButton(text=WEEKDAY_LABELS[i], callback_data=f"{prefix}:{i}")
                for i in range(4, 7)
            ],
        ]
    )


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    if not re.match(r"^\d{1,2}:\d{2}$", text.strip()):
        return None
    hour, minute = map(int, text.strip().split(":"))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _school_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().SCHOOL_TZ)


def _today_local() -> date:
    return datetime.now(_school_tz()).date()


async def _person_by_telegram(
    session: AsyncSession, telegram_user_id: int
) -> Person | None:
    result = await session.execute(
        select(Person).where(Person.telegram_user_id == telegram_user_id)
    )
    return result.scalar_one_or_none()


class _BlockArchivedMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from_user = None
        if isinstance(event, Message) and event.from_user is not None:
            from_user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user is not None:
            from_user = event.from_user
        if from_user is not None and not from_user.is_bot:
            async with async_session_maker() as session:
                person = await _person_by_telegram(session, from_user.id)
            if (
                person is not None
                and person.archived_at is not None
                and person.role != "admin"
            ):
                if isinstance(event, Message) and event.text:
                    if event.text.split(maxsplit=1)[0] == "/start":
                        return await handler(event, data)
                if isinstance(event, Message):
                    await event.answer(ARCHIVED_ACCESS_CLOSED)
                elif isinstance(event, CallbackQuery):
                    await event.answer(ARCHIVED_ACCESS_CLOSED, show_alert=True)
                return None
        return await handler(event, data)


async def _ensure_person(message: Message) -> Person | None:
    if message.from_user is None:
        return None
    async with async_session_maker() as session:
        person = await upsert_from_telegram(
            session,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name or "User",
        )
        await session.commit()
        return person


async def _person_for_callback(query: CallbackQuery) -> Person | None:
    if query.from_user is None:
        return None
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        await session.commit()
        return person


def _is_admin(person: Person) -> bool:
    return person.role == "admin"


async def _require_admin_message(
    message: Message, session: AsyncSession
) -> Person | None:
    if message.from_user is None:
        await message.answer("")
        return None
    person = await _person_by_telegram(session, message.from_user.id)
    if person is None or not _is_admin(person):
        await message.answer(REFUSAL if person and not _is_admin(person) else "")
        return None
    return person


async def _require_admin_message_strict(
    message: Message, session: AsyncSession
) -> Person | None:
    if message.from_user is None:
        await message.answer(REFUSAL)
        return None
    person = await _person_by_telegram(session, message.from_user.id)
    if person is None or not _is_admin(person):
        await message.answer(REFUSAL)
        return None
    return person


async def _require_admin_callback(
    query: CallbackQuery, session: AsyncSession | None = None
) -> Person | None:
    if query.from_user is None:
        await query.answer(REFUSAL, show_alert=True)
        return None
    if session is None:
        person = await _person_for_callback(query)
    else:
        person = await _person_by_telegram(session, query.from_user.id)
    if person is None or not _is_admin(person):
        await query.answer(REFUSAL, show_alert=True)
        return None
    return person


def _parse_school_slot(text: str) -> datetime | None:
    try:
        parsed = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=ZoneInfo(get_settings().SCHOOL_TZ))


async def _send_notice(
    bot: Bot, people: list[Person], text: str
) -> tuple[str, list[str], list[str]]:
    reachable, _missing = countable(people)
    delivered_names: list[str] = []
    failed_names: list[str] = [
        person.full_name for person in people if person.telegram_user_id is None
    ]
    for person in reachable:
        try:
            await bot.send_message(person.telegram_user_id, text)  # type: ignore[arg-type]
            delivered_names.append(person.full_name)
        except Exception:
            logger.exception("Notice failed for %s", person.id)
            failed_names.append(person.full_name)
    delivered = len(delivered_names)
    failed_count = len(people) - delivered
    return delivery_report(delivered, failed_count), delivered_names, failed_names


async def _admin_person(message: Message) -> Person | None:
    if message.from_user is None:
        return None
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)
        if person is None or not _is_admin(person):
            return None
        return person


def reminder_inline_markup(item: DueReminder) -> InlineKeyboardMarkup | None:
    if item.kind in ("attendance_ask", "attendance_nudge") and item.school_session_id:
        sid = item.school_session_id
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data=f"att:{sid}:yes"),
                    InlineKeyboardButton(text="Нет", callback_data=f"att:{sid}:no"),
                ]
            ]
        )
    if item.kind in ("session_start", "guest_day_of") and item.school_session_id:
        sid = item.school_session_id
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Иду", callback_data=f"go:{sid}"),
                    InlineKeyboardButton(text="Не иду", callback_data=f"nogo:{sid}"),
                ]
            ]
        )
    return None


def reminder_reply_keyboard(item: DueReminder) -> ReplyKeyboardMarkup | None:
    """Client bottom menu on session reminders when enrollment message may have failed."""
    if item.kind == "session_start" and item.person_role == "client":
        return reply_markup_for_role("client")
    return None


# --- Generic list pick infrastructure ---


def _pick_ids_from_data(data: dict[str, object]) -> list[str]:
    return list(data.get("pick_ids") or [])  # type: ignore[arg-type]


@dataclass(frozen=True)
class PickSpec:
    is_index_pick: bool
    build_rows: Callable[[AsyncSession, dict[str, object]], Awaitable[list[PickRow]]]
    on_index_pick: (
        Callable[[Message, FSMContext, list[PickRow]], Awaitable[None]] | None
    ) = None
    on_many_pick: (
        Callable[[Message, FSMContext, list[PickRow], Person], Awaitable[None]] | None
    ) = None
    notice_recipient: (
        Callable[[AsyncSession, PickRow], Awaitable[Person | None]] | None
    ) = None
    is_notice_multi: bool = False
    more_callback: str | None = None


REGISTRY: dict[str, PickSpec] = {}


def register_pick_spec(purpose: str, spec: PickSpec) -> None:
    REGISTRY[purpose] = spec


_att_prefix_pick_spec: PickSpec | None = None


def register_att_pick_spec(spec: PickSpec) -> None:
    global _att_prefix_pick_spec
    _att_prefix_pick_spec = spec


def _get_pick_spec(purpose: str) -> PickSpec | None:
    if purpose.startswith("att:"):
        return _att_prefix_pick_spec
    return REGISTRY.get(purpose)


def _list_pick_more_callback(purpose: str) -> str:
    spec = _get_pick_spec(purpose)
    if spec is not None:
        if spec.more_callback is not None:
            return spec.more_callback
        if spec.is_index_pick:
            return f"{purpose}_more"
    return "msg_more"


def _list_pick_hint(purpose: str) -> str:
    spec = _get_pick_spec(purpose)
    if spec is not None:
        return HINT_INDEX if spec.is_index_pick else HINT_MANY
    return HINT_MANY


def _list_pick_keyboard(
    rows: list[PickRow], page: int, purpose: str
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if has_next_page(rows, page):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Дальше",
                    callback_data=_list_pick_more_callback(purpose),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="Отмена", callback_data="pick_cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_list_pick_page(
    message: Message,
    rows: list[PickRow],
    page: int,
    *,
    hint: str,
    title: str | None = None,
    purpose: str,
) -> None:
    body = page_text(rows, page, hint)
    text = f"{title}\n\n{body}" if title else body
    await message.answer(
        text, reply_markup=_list_pick_keyboard(rows, page, purpose)
    )


async def _start_list_pick(
    message: Message,
    state: FSMContext,
    *,
    purpose: str,
    rows: list[PickRow],
    title: str | None = None,
    hint: str = HINT_MANY,
) -> None:
    await state.set_state(ListPickFSM.waiting)
    await state.update_data(
        pick_ids=[row.id for row in rows],
        pick_page=0,
        pick_purpose=purpose,
    )
    await _send_list_pick_page(
        message, rows, 0, hint=hint, title=title, purpose=purpose
    )


async def _list_pick_rows_from_data(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    purpose = str(data.get("pick_purpose") or "")
    spec = _get_pick_spec(purpose)
    if spec is None:
        return []
    return await spec.build_rows(session, data)
