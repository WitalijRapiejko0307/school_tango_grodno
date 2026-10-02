"""Telegram bot handlers (aiogram 3) — UX variant A."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import async_session_maker
from app.models import (
    Attendance,
    WeeklySlot,
    ContactCard,
    Group,
    GroupMembership,
    Person,
    Product,
    Reminder,
    SchoolSession,
    Stream,
)
from app.services.attendance import mark_attendance
from app.services.billing import (
    active_subscription,
    create_product,
    current_price,
    list_active_products_with_prices,
    open_subscription,
    set_price,
)
from app.services.identity import (
    has_pending_phone_invite,
    invite_admin,
    upsert_from_telegram,
)
from app.services.bot_messages import bind_outbound_school_session_id, reset_outbound_context
from app.services.notifications import (
    DueReminder,
    answer_attendance,
    find_today_own_session_conflict,
    guest_day_summary,
    guest_rsvp,
    list_today_session_reminder_lines,
    record_coming,
    record_not_coming,
    second_booking_prompt_text,
)
from app.services.school_time import format_school_date, format_school_datetime
from app.services.schedule import (
    WEEKDAY_LABELS,
    active_group_membership,
    assign_group,
    cancel_occurrence,
    create_group,
    create_stream,
    create_weekly_slot,
    group_transfer_confirmation_text,
    materialize_range,
    override_occurrence,
    parse_dmy,
    set_group_stream,
    update_weekly_slot,
)

logger = logging.getLogger(__name__)

router = Router(name="telegram")

# --- Menu labels (variant A) ---

BTN_TODAY = "Сегодня"
BTN_GROUPS = "Группы"
BTN_PEOPLE = "Люди"
BTN_PRICE = "Прайс"
BTN_REMINDERS = "Напоминания"
BTN_GUESTS = "Гости"
BTN_ADMINS = "Админы"
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
    BTN_GUESTS,
    BTN_ADMINS,
]
CLIENT_MENU = [BTN_PRICE, BTN_MY_GROUP, BTN_OTHER_GROUP, BTN_BALANCE]
GUEST_MENU = [BTN_SCHEDULE, BTN_CONTACTS]

REFUSAL = "Эта команда доступна только администраторам."


def role_keyboard(role: str) -> list[str]:
    """Flat list of reply-keyboard button texts for the role."""
    if role == "admin":
        return list(ADMIN_MENU)
    if role == "client":
        return list(CLIENT_MENU)
    return list(GUEST_MENU)


BTN_SHARE_PHONE = "Подтвердить телефон"


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
    if role == "admin":
        rows = [
            [KeyboardButton(text=texts[0]), KeyboardButton(text=texts[1])],
            [KeyboardButton(text=texts[2]), KeyboardButton(text=texts[3])],
            [KeyboardButton(text=texts[4]), KeyboardButton(text=texts[5])],
            [KeyboardButton(text=texts[6])],
        ]
    elif role == "client":
        rows = [
            [KeyboardButton(text=texts[0]), KeyboardButton(text=texts[1])],
            [KeyboardButton(text=texts[2]), KeyboardButton(text=texts[3])],
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


async def _list_today_sessions(session: AsyncSession, day: date) -> list[SchoolSession]:
    tz = _school_tz()
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    start_utc = day_start.astimezone(UTC)
    end_utc = day_end.astimezone(UTC)
    await materialize_range(session, day, day + timedelta(days=1))
    result = await session.execute(
        select(SchoolSession)
        .where(
            SchoolSession.starts_at >= start_utc,
            SchoolSession.starts_at < end_utc,
            SchoolSession.status == "scheduled",
        )
        .order_by(SchoolSession.starts_at)
    )
    return list(result.scalars().all())


async def _session_group_name(session: AsyncSession, group_id: str) -> str:
    result = await session.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one()
    return group.name


async def _members_with_attendance(
    session: AsyncSession, group_id: str, school_session_id: str
) -> list[tuple[Person, Attendance | None]]:
    mem_result = await session.execute(
        select(Person)
        .join(GroupMembership, GroupMembership.person_id == Person.id)
        .where(
            GroupMembership.group_id == group_id,
            GroupMembership.ended_on.is_(None),
        )
        .order_by(Person.full_name)
    )
    people = list(mem_result.scalars().all())
    out: list[tuple[Person, Attendance | None]] = []
    for person in people:
        att_result = await session.execute(
            select(Attendance).where(
                Attendance.person_id == person.id,
                Attendance.session_id == school_session_id,
            )
        )
        out.append((person, att_result.scalar_one_or_none()))
    return out


def _attendance_list_text(
    members: list[tuple[Person, Attendance | None]], group_name: str, when: str
) -> str:
    lines = [f"Отметка: {group_name}, {when}", ""]
    for person, att in members:
        mark = "✓" if att is not None else "·"
        src = f" ({att.source})" if att else ""
        lines.append(f"{mark} {person.full_name}{src}")
    lines.append("")
    lines.append("Нажмите имя, чтобы переключить отметку.")
    return "\n".join(lines)


def _attendance_keyboard(
    school_session_id: str, members: list[tuple[Person, Attendance | None]]
) -> InlineKeyboardMarkup:
    buttons = []
    for person, att in members:
        mark = "✓ " if att else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{person.full_name}",
                    callback_data=f"adm_att:{school_session_id}:{person.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _active_membership(
    session: AsyncSession, person_id: str
) -> GroupMembership | None:
    return await active_group_membership(session, person_id)


async def _stream_pick_keyboard(
    session: AsyncSession,
    *,
    mode: str,
    group_id: str | None = None,
) -> InlineKeyboardMarkup:
    """mode: new_group | edit_group — callback prefixes new_grp_strm / set_grp_strm."""
    streams = list(
        (await session.execute(select(Stream).order_by(Stream.name))).scalars().all()
    )
    rows: list[list[InlineKeyboardButton]] = []
    if mode == "new_group":
        for stream in streams:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stream.name,
                        callback_data=f"new_grp_strm:{stream.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Создать поток", callback_data="new_grp_strm:new"
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="Без потока", callback_data="new_grp_strm:none")]
        )
    else:
        assert group_id is not None
        for stream in streams:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stream.name,
                        callback_data=f"set_grp_strm:{group_id}:{stream.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Создать поток",
                    callback_data=f"set_grp_strm:{group_id}:new",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Без потока", callback_data=f"set_grp_strm:{group_id}:none"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _finish_assign_group(
    query: CallbackQuery,
    state: FSMContext,
    person_id: str,
    group_id: str,
) -> None:
    today = _today_local()
    notify_id: int | None = None
    async with async_session_maker() as session:
        target = await session.get(Person, person_id)
        was_guest = target is not None and target.role == "guest"
        await assign_group(session, person_id, group_id, today)
        await session.commit()
        if was_guest and target is not None and target.telegram_user_id is not None:
            notify_id = target.telegram_user_id
    await state.clear()
    await query.answer("Готово")
    if query.message:
        await query.message.answer("Человек назначен в группу.")
    if notify_id is not None:
        try:
            await query.bot.send_message(
                notify_id,
                "Вас записали в группу. Теперь вы клиент школы.",
                reply_markup=reply_markup_for_role("client"),
            )
        except Exception:
            logger.exception("Failed to refresh client menu for %s", notify_id)


async def _format_contacts(session: AsyncSession) -> str:
    result = await session.execute(select(ContactCard).order_by(ContactCard.name))
    cards = list(result.scalars().all())
    if not cards:
        return "Контакты пока не добавлены."
    lines = ["Контакты:"]
    for card in cards:
        lines.append(f"• {card.name} — {card.phone} ({card.role_label})")
    return "\n".join(lines)


async def _client_mark_yes(
    session: AsyncSession,
    person_id: str,
    school_session_id: str,
    now: datetime,
) -> str:
    """Yes-path for cross-group visit: answer_attendance if ask exists, else mark."""
    ask = await session.execute(
        select(Reminder).where(
            Reminder.person_id == person_id,
            Reminder.kind == "attendance_ask",
            Reminder.session_id == school_session_id,
        )
    )
    if ask.scalar_one_or_none() is not None:
        return await answer_attendance(session, person_id, school_session_id, True, now)
    att_before = await session.execute(
        select(Attendance).where(
            Attendance.person_id == person_id,
            Attendance.session_id == school_session_id,
        )
    )
    existing = att_before.scalar_one_or_none()
    if existing is not None and existing.source == "admin":
        return "Уже отмечено администратором."
    await mark_attendance(session, person_id, school_session_id, "client")
    return "Отметили по вашему ответу."


# --- FSM ---


class AdminInviteFSM(StatesGroup):
    waiting_identifier = State()


class ContactFSM(StatesGroup):
    name = State()
    phone = State()
    role_label = State()


class NewGroupFSM(StatesGroup):
    name = State()
    new_stream_name = State()


class GroupStreamFSM(StatesGroup):
    new_stream_name = State()


class SlotFSM(StatesGroup):
    group_id = State()
    date_str = State()
    time_str = State()
    duration = State()
    place = State()
    bring_notes = State()


class GuestPickFSM(StatesGroup):
    number = State()


class WeekFSM(StatesGroup):
    time_str = State()
    duration = State()
    place = State()
    notes = State()
    once_date = State()


class AssignGroupFSM(StatesGroup):
    person_id = State()
    group_id = State()


class SubProductFSM(StatesGroup):
    name = State()
    lessons = State()
    validity = State()
    price = State()


class DropInProductFSM(StatesGroup):
    name = State()
    price = State()


class OpenSubFSM(StatesGroup):
    person_id = State()
    product_id = State()


class NewPriceFSM(StatesGroup):
    product_id = State()
    amount = State()
    valid_from = State()


class AdminMenuFSM(StatesGroup):
    choosing = State()


# --- /start ---


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    person = await _ensure_person(message)
    if person is None:
        return
    markup = reply_markup_for_role(person.role)
    text = f"Здравствуйте, {person.full_name}!"
    if person.role == "guest":
        async with async_session_maker() as session:
            ask_phone = await has_pending_phone_invite(session)
        if ask_phone:
            markup = _guest_keyboard_with_phone()
            text += (
                "\n\nЕсли вас пригласили администратором, нажмите «Подтвердить телефон» "
                "и отправьте свой номер. Иначе откройте расписание как гость."
            )
    await message.answer(text, reply_markup=markup)


@router.message(F.contact)
async def on_contact(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.contact is None:
        return
    contact = message.contact
    if contact.user_id is not None and contact.user_id != message.from_user.id:
        await message.answer("Отправьте свой номер кнопкой «Подтвердить телефон».")
        return
    await state.clear()
    async with async_session_maker() as session:
        person = await upsert_from_telegram(
            session,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name or "User",
            phone=contact.phone_number,
        )
        await session.commit()
        role = person.role
        name = person.full_name
    if role == "admin":
        await message.answer(
            f"{name}, вы администратор.",
            reply_markup=reply_markup_for_role("admin"),
        )
        return
    if role == "client":
        await message.answer(
            f"{name}, номер сохранён.",
            reply_markup=reply_markup_for_role("client"),
        )
        return
    await message.answer(
        "Этот номер не найден среди приглашений администраторов.",
        reply_markup=reply_markup_for_role("guest"),
    )


# --- Callbacks: go, att, rsvp, admin attendance ---


@router.callback_query(F.data.startswith("go:"))
async def cb_go(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала нажмите /start")
            return
        await record_coming(session, person.id, session_id, datetime.now(UTC))
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer("записали, что идёте")


@router.callback_query(F.data.startswith("nogo:"))
async def cb_not_go(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала нажмите /start")
            return
        await record_not_coming(session, person.id, session_id, datetime.now(UTC))
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer("приняли, что не идёте")


@router.callback_query(F.data.startswith("att:"))
async def cb_attendance(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    _, session_id, answer = parts
    yes = answer == "yes"
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала нажмите /start")
            return
        try:
            text = await answer_attendance(
                session, person.id, session_id, yes, datetime.now(UTC)
            )
        except ValueError:
            text = "Занятие не списываем." if not yes else "Нет активного вопроса."
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


@router.callback_query(F.data.startswith("rsvp:"))
async def cb_rsvp(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала нажмите /start")
            return
        await guest_rsvp(session, person.id, session_id)
        await session.commit()
    await query.answer("Записали!")
    if query.message:
        await query.message.answer("Вы записаны на занятие.")


@router.callback_query(F.data.startswith("adm_sess:"))
async def cb_admin_session(query: CallbackQuery) -> None:
    if query.data is None:
        return
    school_session_id = query.data.split(":", 1)[1]
    person = await _person_for_callback(query)
    if person is None or not _is_admin(person):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        sess_result = await session.execute(
            select(SchoolSession).where(SchoolSession.id == school_session_id)
        )
        school_session = sess_result.scalar_one()
        group_name = await _session_group_name(session, school_session.group_id)
        members = await _members_with_attendance(
            session, school_session.group_id, school_session_id
        )
        text = _attendance_list_text(
            members, group_name, format_school_datetime(school_session.starts_at)
        )
        kb = _attendance_keyboard(school_session_id, members)
    await query.answer()
    if query.message:
        await query.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm_att:"))
async def cb_admin_toggle_att(query: CallbackQuery) -> None:
    if query.data is None:
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    _, school_session_id, person_id = parts
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        att_result = await session.execute(
            select(Attendance).where(
                Attendance.person_id == person_id,
                Attendance.session_id == school_session_id,
            )
        )
        existing = att_result.scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)
        else:
            await mark_attendance(session, person_id, school_session_id, "admin")
        sess_result = await session.execute(
            select(SchoolSession).where(SchoolSession.id == school_session_id)
        )
        school_session = sess_result.scalar_one()
        group_name = await _session_group_name(session, school_session.group_id)
        members = await _members_with_attendance(
            session, school_session.group_id, school_session_id
        )
        await session.commit()
        text = _attendance_list_text(
            members, group_name, format_school_datetime(school_session.starts_at)
        )
        kb = _attendance_keyboard(school_session_id, members)
    await query.answer()
    if query.message:
        await query.message.edit_text(text, reply_markup=kb)


async def _complete_other_group_visit(
    session: AsyncSession, person_id: str, target_session_id: str, now: datetime
) -> str:
    return await _client_mark_yes(session, person_id, target_session_id, now)


@router.callback_query(F.data.startswith("other_sess:"))
async def cb_other_group_session(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала /start")
            return
        today = _today_local()
        conflict = await find_today_own_session_conflict(
            session, person.id, session_id, today
        )
        if conflict is not None:
            own_session, group = conflict
            prompt = second_booking_prompt_text(group.name, own_session.starts_at)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отменить и записаться",
                            callback_data=(
                                f"other_swap:{session_id}:{own_session.id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Оставить обе",
                            callback_data=f"other_both:{session_id}",
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data="other_back")],
                ]
            )
            await query.answer()
            if query.message:
                await query.message.answer(prompt, reply_markup=kb)
            return
        text = await _complete_other_group_visit(
            session, person.id, session_id, datetime.now(UTC)
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


@router.callback_query(F.data.startswith("other_swap:"))
async def cb_other_group_swap(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    _, target_id, own_id = parts
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала /start")
            return
        now = datetime.now(UTC)
        await record_not_coming(session, person.id, own_id, now)
        text = await _complete_other_group_visit(
            session, person.id, target_id, now
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


@router.callback_query(F.data.startswith("other_both:"))
async def cb_other_group_both(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    target_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None:
            await query.answer("Сначала /start")
            return
        text = await _complete_other_group_visit(
            session, person.id, target_id, datetime.now(UTC)
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


@router.callback_query(F.data == "other_back")
async def cb_other_group_back(query: CallbackQuery) -> None:
    await query.answer("Отменено")


# --- Inline pick helpers (groups, products, people) ---


@router.callback_query(F.data.startswith("pick_grp:"))
async def cb_pick_group_slot(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    person = await _person_for_callback(query)
    if person is None or not _is_admin(person):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.update_data(mode="new", group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            "День недели:",
            reply_markup=_weekday_keyboard("wday"),
        )


@router.callback_query(F.data.startswith("pick_person:"))
async def cb_pick_person_assign(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        groups = list(
            (await session.execute(select(Group).order_by(Group.name))).scalars().all()
        )
    if not groups:
        await query.answer("Нет групп", show_alert=True)
        return
    await state.set_state(AssignGroupFSM.group_id)
    await state.update_data(person_id=person_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=g.name, callback_data=f"assign_grp:{g.id}")]
            for g in groups
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите группу:", reply_markup=kb)


@router.callback_query(F.data.startswith("assign_grp:"))
async def cb_assign_group(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    data = await state.get_data()
    person_id = data.get("person_id")
    if not person_id:
        await query.answer("Сначала выберите человека")
        return
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        target = await session.get(Person, person_id)
        new_group = await session.get(Group, group_id)
        if target is None or new_group is None:
            await query.answer("Не найдено", show_alert=True)
            return
        membership = await _active_membership(session, person_id)
        if membership is not None and membership.group_id == group_id:
            await query.answer("Уже в этой группе", show_alert=True)
            return
        if membership is not None:
            old_group = await session.get(Group, membership.group_id)
            if old_group is None:
                await query.answer("Ошибка", show_alert=True)
                return
            text = group_transfer_confirmation_text(
                target.full_name,
                old_group.name,
                membership.started_on,
                new_group.name,
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Перевести",
                            callback_data=f"assign_go:{person_id}:{group_id}",
                        )
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="assign_cancel")],
                ]
            )
            await query.answer()
            if query.message:
                await query.message.answer(text, reply_markup=kb)
            return
    await _finish_assign_group(query, state, person_id, group_id)


@router.callback_query(F.data.startswith("assign_go:"))
async def cb_assign_group_confirm(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    _, person_id, group_id = query.data.split(":", 2)
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await _finish_assign_group(query, state, person_id, group_id)


@router.callback_query(F.data == "assign_cancel")
async def cb_assign_group_cancel(query: CallbackQuery) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Перевод в другую группу отменён.")


@router.callback_query(F.data.startswith("open_sub_p:"))
async def cb_open_sub_person(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        products = list(
            (
                await session.execute(
                    select(Product).where(
                        Product.kind == "subscription", Product.active.is_(True)
                    )
                )
            ).scalars().all()
        )
    if not products:
        await query.answer("Нет абонементов", show_alert=True)
        return
    await state.set_state(OpenSubFSM.product_id)
    await state.update_data(person_id=person_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p.name, callback_data=f"open_sub_prod:{p.id}")]
            for p in products
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите абонемент:", reply_markup=kb)


@router.callback_query(F.data.startswith("open_sub_prod:"))
async def cb_open_sub_product(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    data = await state.get_data()
    person_id = data.get("person_id")
    if not person_id:
        await query.answer("Ошибка")
        return
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    today = _today_local()
    async with async_session_maker() as session:
        await open_subscription(session, person_id, product_id, today)
        await session.commit()
    await state.clear()
    await query.answer("Выдано")
    if query.message:
        await query.message.answer("Абонемент выдан.")


@router.callback_query(F.data.startswith("new_price_p:"))
async def cb_new_price_product(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(NewPriceFSM.amount)
    await state.update_data(product_id=product_id)
    await query.answer()
    if query.message:
        await query.message.answer("Новая сумма (например 120.00):")


@router.callback_query(F.data == "admin_new_group")
async def cb_admin_new_group(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(NewGroupFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название новой группы:")


@router.callback_query(F.data == "admin_slot")
async def cb_admin_slot(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        groups = list(
            (await session.execute(select(Group).order_by(Group.name))).scalars().all()
        )
    if not groups:
        await query.answer("Сначала создайте группу", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=g.name, callback_data=f"pick_grp:{g.id}")]
            for g in groups
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите группу для слота:", reply_markup=kb)


@router.callback_query(F.data == "people_assign")
async def cb_people_assign(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        people = list(
            (
                await session.execute(
                    select(Person).where(Person.role != "admin").order_by(Person.full_name)
                )
            ).scalars().all()
        )
    if not people:
        await query.answer("Нет людей", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=p.full_name,
                    callback_data=f"pick_person:{p.id}",
                )
            ]
            for p in people
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите человека:", reply_markup=kb)


@router.callback_query(F.data == "price_sub")
async def cb_price_sub(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(SubProductFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название абонемента:")


@router.callback_query(F.data == "price_dropin")
async def cb_price_dropin(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(DropInProductFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название разового занятия:")


@router.callback_query(F.data == "price_open_sub")
async def cb_price_open_sub(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        clients = list(
            (
                await session.execute(
                    select(Person).where(Person.role == "client").order_by(Person.full_name)
                )
            ).scalars().all()
        )
    if not clients:
        await query.answer("Нет клиентов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=c.full_name, callback_data=f"open_sub_p:{c.id}")]
            for c in clients
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Кому выдать абонемент?", reply_markup=kb)


@router.callback_query(F.data == "price_new_price")
async def cb_price_new_price(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        products = list(
            (await session.execute(select(Product).order_by(Product.name))).scalars().all()
        )
    if not products:
        await query.answer("Нет продуктов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p.name, callback_data=f"new_price_p:{p.id}")]
            for p in products
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Для какого продукта новая цена?", reply_markup=kb)


@router.callback_query(F.data == "admin_invite")
async def cb_admin_invite(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(AdminInviteFSM.waiting_identifier)
    await query.answer()
    if query.message:
        await query.message.answer("Введите @username или телефон будущего админа:")


@router.callback_query(F.data == "admin_contact")
async def cb_admin_contact(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(ContactFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Имя контакта:")


# --- Reply menu handlers ---


@router.message(F.text == BTN_TODAY)
async def menu_today(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None:
            return
        if not _is_admin(person):
            await message.answer(REFUSAL)
            return
        today = _today_local()
        sessions = await _list_today_sessions(session, today)
        await session.commit()
        if not sessions:
            await message.answer("На сегодня занятий нет.")
            return
        lines = ["Занятия сегодня:"]
        buttons = []
        for s in sessions:
            gname = await _session_group_name(session, s.group_id)
            lines.append(f"• {gname} — {format_school_datetime(s.starts_at)}, {s.place}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"Отметить: {gname}",
                        callback_data=f"adm_sess:{s.id}",
                    )
                ]
            )
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text == BTN_GROUPS)
async def menu_groups(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or not _is_admin(person):
            await message.answer(REFUSAL if person and not _is_admin(person) else "")
            return
        groups = list(
            (await session.execute(select(Group).order_by(Group.name))).scalars().all()
        )
    text = "Группы:\n" + ("\n".join(f"• {g.name}" for g in groups) if groups else "пока нет")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Новая группа", callback_data="admin_new_group")],
            [
                InlineKeyboardButton(
                    text="Поток группы", callback_data="admin_group_stream"
                )
            ],
            [InlineKeyboardButton(text="Недельный слот", callback_data="admin_slot")],
            [InlineKeyboardButton(text="Изменить график", callback_data="admin_edit_week")],
            [InlineKeyboardButton(text="Одно занятие", callback_data="admin_edit_once")],
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_PEOPLE)
async def menu_people(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or not _is_admin(person):
            await message.answer(REFUSAL if person and not _is_admin(person) else "")
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="В группу", callback_data="people_assign")],
        ]
    )
    await message.answer("Люди — выберите действие:", reply_markup=kb)


@router.message(F.text == BTN_PRICE)
async def menu_price(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None:
            return
        today = _today_local()
        items = await list_active_products_with_prices(session, today)
        lines = ["Прайс:"]
        for product, price in items:
            kind = "абонемент" if product.kind == "subscription" else "разовое"
            extra = ""
            if product.lessons_count:
                extra = f", {product.lessons_count} зан., {product.validity_days} дн."
            lines.append(f"• {product.name} ({kind}{extra}): {price.amount}")
        text = "\n".join(lines) if items else "Прайс пока пуст."
        if _is_admin(person):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Абонемент", callback_data="price_sub")],
                    [InlineKeyboardButton(text="Разовое", callback_data="price_dropin")],
                    [
                        InlineKeyboardButton(
                            text="Выдать абонемент", callback_data="price_open_sub"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Новая цена", callback_data="price_new_price"
                        )
                    ],
                ]
            )
            await message.answer(text, reply_markup=kb)
        else:
            await message.answer(text)


@router.message(F.text == BTN_REMINDERS)
async def menu_reminders(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or not _is_admin(person):
            await message.answer(REFUSAL if person and not _is_admin(person) else "")
            return
        today = _today_local()
        lines = await list_today_session_reminder_lines(session, today)
        if not lines:
            await message.answer("На сегодня реакций на напоминания пока нет.")
            return
        await message.answer("\n".join(lines))


@router.message(F.text == BTN_GUESTS)
async def menu_guests_summary(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or not _is_admin(person):
            await message.answer(REFUSAL if person and not _is_admin(person) else "")
            return
        today = _today_local()
        rows = await guest_day_summary(session, today)
        if not rows:
            await message.answer("Гостей на сегодня нет.")
            return
        lines = ["Гости сегодня:"]
        for rsvp, guest, school_session in rows:
            phone = guest.phone or "—"
            lines.append(
                f"• {guest.full_name} ({phone}) — "
                f"{format_school_datetime(school_session.starts_at)}, {school_session.place}"
            )
        await message.answer("\n".join(lines))


@router.message(F.text == BTN_ADMINS)
async def menu_admins(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or not _is_admin(person):
            await message.answer(REFUSAL if person and not _is_admin(person) else "")
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить админа", callback_data="admin_invite")],
            [InlineKeyboardButton(text="Контакт", callback_data="admin_contact")],
        ]
    )
    await message.answer("Админы и контакты:", reply_markup=kb)


@router.message(F.text == BTN_MY_GROUP)
async def menu_my_group(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or person.role != "client":
            if person and person.role == "admin":
                await message.answer(REFUSAL)
            return
        membership = await _active_membership(session, person.id)
        if membership is None:
            await message.answer("Вы пока не в группе.")
            return
        gname = await _session_group_name(session, membership.group_id)
        today = _today_local()
        await materialize_range(session, today, today + timedelta(days=21))
        await session.commit()
        now = datetime.now(UTC)
        next_result = await session.execute(
            select(SchoolSession)
            .where(
                SchoolSession.group_id == membership.group_id,
                SchoolSession.starts_at > now,
                SchoolSession.status == "scheduled",
            )
            .order_by(SchoolSession.starts_at)
            .limit(1)
        )
        nxt = next_result.scalar_one_or_none()
        if nxt:
            await message.answer(
                f"Группа: {gname}\nБлижайшее: {format_school_datetime(nxt.starts_at)}, {nxt.place}"
            )
        else:
            await message.answer(f"Группа: {gname}\nБлижайших занятий нет.")


@router.message(F.text == BTN_BALANCE)
async def menu_balance(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or person.role != "client":
            return
        today = _today_local()
        sub = await active_subscription(session, person.id, today)
        if sub is None:
            await message.answer("нет абонемента")
        else:
            await message.answer(
                f"Осталось занятий: {sub.lessons_left}, действует до {format_school_date(sub.valid_until)}"
            )


@router.message(F.text == BTN_OTHER_GROUP)
async def menu_other_group(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None or person.role != "client":
            return
        membership = await _active_membership(session, person.id)
        my_group_id = membership.group_id if membership else None
        today = _today_local()
        sessions = await _list_today_sessions(session, today)
        await session.commit()
        sessions = [s for s in sessions if s.group_id != my_group_id]
        if not sessions:
            await message.answer("Сегодня нет других занятий.")
            return
        buttons = []
        for s in sessions:
            gname = await _session_group_name(session, s.group_id)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{gname} {format_school_datetime(s.starts_at)}",
                        callback_data=f"other_sess:{s.id}",
                    )
                ]
            )
        await message.answer(
            "Выберите занятие:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.message(F.text == BTN_SCHEDULE)
async def menu_schedule(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None:
            return
        now_local = datetime.now(_school_tz())
        today = now_local.date()
        week_end = today + timedelta(days=7)
        await materialize_range(session, today, week_end)
        week_end_utc = datetime(
            week_end.year, week_end.month, week_end.day, tzinfo=_school_tz()
        ).astimezone(UTC)
        result = await session.execute(
            select(SchoolSession)
            .where(
                SchoolSession.status == "scheduled",
                SchoolSession.starts_at >= now_local.astimezone(UTC),
                SchoolSession.starts_at < week_end_utc,
            )
            .order_by(SchoolSession.starts_at)
        )
        sessions = list(result.scalars().all())
        await session.commit()
        if not sessions:
            contacts = await _format_contacts(session)
            await message.answer(
                "На ближайшие 7 дней занятий нет. Ниже только предстоящая неделя, не весь месяц.\n\n"
                + contacts
            )
            return
        lines = [
            "Расписание на ближайшие 7 дней.",
            "Показана только предстоящая неделя, не весь месяц.",
            "Напишите номер занятия, на которое придёте.",
        ]
        choices: dict[str, str] = {}
        for index, school_session in enumerate(sessions, start=1):
            gname = await _session_group_name(session, school_session.group_id)
            local_t = format_school_datetime(school_session.starts_at)
            lines.append(f"{index}. {local_t} — {gname}, {school_session.place}")
            choices[str(index)] = school_session.id
        await state.set_state(GuestPickFSM.number)
        await state.update_data(guest_choices=choices)
        await message.answer("\n".join(lines))


@router.message(GuestPickFSM.number, F.text.regexp(r"^\s*\d+\s*$"))
async def fsm_guest_pick(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    data = await state.get_data()
    choices = data.get("guest_choices") or {}
    session_id = choices.get(message.text.strip())
    if session_id is None:
        await message.answer("Нет такого номера. Напишите число из списка.")
        return
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)
        if person is None:
            await state.clear()
            await message.answer("Сначала нажмите /start")
            return
        await guest_rsvp(session, person.id, session_id)
        school_session = await session.get(SchoolSession, session_id)
        gname = ""
        when = ""
        if school_session is not None:
            gname = await _session_group_name(session, school_session.group_id)
            when = format_school_datetime(school_session.starts_at)
        await session.commit()
    await state.clear()
    await message.answer(f"Вы записаны: {when}, {gname}.")


@router.message(F.text == BTN_CONTACTS)
async def menu_contacts(message: Message) -> None:
    async with async_session_maker() as session:
        await message.answer(await _format_contacts(session))


# --- FSM text steps ---


@router.message(AdminInviteFSM.waiting_identifier)
async def fsm_admin_invite(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    text = message.text.strip()
    username = None
    phone = None
    if text.startswith("@") or re.match(r"^[A-Za-z0-9_]{3,}$", text.lstrip("@")):
        username = text if text.startswith("@") else f"@{text}"
    else:
        phone = text
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        invite = await invite_admin(session, admin, username=username, phone=phone)
        await session.commit()
        status = "активен" if invite.status == "active" else "ожидает входа в бота"
        await message.answer(f"Приглашение создано ({status}).")
    await state.clear()


@router.message(ContactFSM.name)
async def fsm_contact_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(contact_name=message.text.strip())
    await state.set_state(ContactFSM.phone)
    await message.answer("Телефон:")


@router.message(ContactFSM.phone)
async def fsm_contact_phone(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(contact_phone=message.text.strip())
    await state.set_state(ContactFSM.role_label)
    await message.answer("Роль (подпись):")


@router.message(ContactFSM.role_label)
async def fsm_contact_role(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        card = ContactCard(
            name=data["contact_name"],
            phone=data["contact_phone"],
            role_label=message.text.strip(),
        )
        session.add(card)
        await session.commit()
    await message.answer("Контакт сохранён.")
    await state.clear()


@router.message(NewGroupFSM.name)
async def fsm_new_group(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        await state.update_data(pending_group_name=message.text.strip())
        kb = await _stream_pick_keyboard(session, mode="new_group")
    await message.answer("Выберите поток для группы:", reply_markup=kb)


@router.callback_query(F.data.startswith("new_grp_strm:"))
async def cb_new_group_stream(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    choice = query.data.split(":", 1)[1]
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    data = await state.get_data()
    group_name = data.get("pending_group_name")
    if not group_name:
        await query.answer("Сначала введите название группы", show_alert=True)
        return
    if choice == "new":
        await state.set_state(NewGroupFSM.new_stream_name)
        await query.answer()
        if query.message:
            await query.message.answer("Название нового потока:")
        return
    stream_id: str | None = None if choice == "none" else choice
    async with async_session_maker() as session:
        group = await create_group(session, group_name, stream_id=stream_id)
        await session.commit()
        stream_note = ""
        if stream_id:
            stream = await session.get(Stream, stream_id)
            if stream is not None:
                stream_note = f", поток «{stream.name}»"
    await state.clear()
    await query.answer("Создано")
    if query.message:
        await query.message.answer(f"Группа «{group.name}» создана{stream_note}.")


@router.callback_query(F.data == "admin_group_stream")
async def cb_admin_group_stream(query: CallbackQuery) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        groups = list(
            (await session.execute(select(Group).order_by(Group.name))).scalars().all()
        )
    if not groups:
        await query.answer("Сначала создайте группу", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=g.name, callback_data=f"pick_grp_strm:{g.id}"
                )
            ]
            for g in groups
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите группу для потока:", reply_markup=kb)


@router.callback_query(F.data.startswith("pick_grp_strm:"))
async def cb_pick_group_stream(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None:
            await query.answer("Не найдено", show_alert=True)
            return
        kb = await _stream_pick_keyboard(session, mode="edit_group", group_id=group_id)
    await state.update_data(edit_group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Поток для группы «{group.name}»:", reply_markup=kb
        )


@router.callback_query(F.data.startswith("set_grp_strm:"))
async def cb_set_group_stream(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, group_id, choice = parts
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    if choice == "new":
        await state.set_state(GroupStreamFSM.new_stream_name)
        await state.update_data(edit_group_id=group_id)
        await query.answer()
        if query.message:
            await query.message.answer("Название нового потока:")
        return
    stream_id: str | None = None if choice == "none" else choice
    async with async_session_maker() as session:
        group = await set_group_stream(session, group_id, stream_id)
        await session.commit()
        if stream_id:
            stream = await session.get(Stream, stream_id)
            stream_label = f"«{stream.name}»" if stream else "выбран"
        else:
            stream_label = "не задан"
    await state.clear()
    await query.answer("Сохранено")
    if query.message:
        await query.message.answer(
            f"Для группы «{group.name}» поток {stream_label}."
        )


@router.message(NewGroupFSM.new_stream_name)
async def fsm_new_group_stream_name(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    group_name = data.get("pending_group_name")
    if not group_name:
        await message.answer("Сначала создайте группу через «Новая группа».")
        await state.clear()
        return
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        stream = await create_stream(session, message.text.strip())
        group = await create_group(session, group_name, stream_id=stream.id)
        await session.commit()
    await state.clear()
    await message.answer(
        f"Группа «{group.name}» создана, поток «{stream.name}»."
    )


@router.message(GroupStreamFSM.new_stream_name)
async def fsm_edit_group_stream_name(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    group_id = data.get("edit_group_id")
    if not group_id:
        await message.answer("Сначала выберите группу.")
        await state.clear()
        return
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        stream = await create_stream(session, message.text.strip())
        group = await set_group_stream(session, group_id, stream.id)
        await session.commit()
    await state.clear()
    await message.answer(
        f"Для группы «{group.name}» задан поток «{stream.name}»."
    )


async def _slot_button_rows(session: AsyncSession, prefix: str) -> list[list[InlineKeyboardButton]]:
    rows = (
        await session.execute(
            select(WeeklySlot, Group)
            .join(Group, WeeklySlot.group_id == Group.id)
            .where(WeeklySlot.active.is_(True))
            .order_by(Group.name, WeeklySlot.weekday, WeeklySlot.start_time)
        )
    ).all()
    buttons = []
    for slot, group in rows:
        label = (
            f"{group.name} {WEEKDAY_LABELS[slot.weekday]} "
            f"{slot.start_time.strftime('%H:%M')}"
        )
        if slot.notes:
            label = f"{label} ({slot.notes})"
        buttons.append(
            [InlineKeyboardButton(text=label[:60], callback_data=f"{prefix}:{slot.id}")]
        )
    return buttons


@router.callback_query(F.data.startswith("wday:"))
async def cb_weekday(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    weekday = int(query.data.split(":", 1)[1])
    await state.update_data(weekday=weekday)
    await state.set_state(WeekFSM.time_str)
    await query.answer()
    if query.message:
        await query.message.answer("Время начала (ЧЧ:ММ):")


@router.callback_query(F.data == "admin_edit_week")
async def cb_admin_edit_week(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        buttons = await _slot_button_rows(session, "wedit")
    if not buttons:
        await query.answer("Нет недельных слотов", show_alert=True)
        return
    await state.update_data(mode="edit_all")
    await query.answer()
    if query.message:
        await query.message.answer(
            "Какой слот изменить на все недели?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data.startswith("wedit:"))
async def cb_wedit(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    slot_id = query.data.split(":", 1)[1]
    await state.update_data(mode="edit_all", slot_id=slot_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            "Новый день недели:",
            reply_markup=_weekday_keyboard("wday"),
        )


@router.callback_query(F.data == "admin_edit_once")
async def cb_admin_edit_once(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    async with async_session_maker() as session:
        buttons = await _slot_button_rows(session, "wonce")
    if not buttons:
        await query.answer("Нет недельных слотов", show_alert=True)
        return
    await query.answer()
    if query.message:
        await query.message.answer(
            "Какое повторяющееся занятие изменить на одну дату?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data.startswith("wonce:"))
async def cb_wonce(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    slot_id = query.data.split(":", 1)[1]
    await state.update_data(mode="edit_one", slot_id=slot_id)
    await state.set_state(WeekFSM.once_date)
    await query.answer()
    if query.message:
        await query.message.answer("Дата занятия (ДД-ММ-ГГГГ):")


@router.message(WeekFSM.once_date)
async def fsm_once_date(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        on_date = parse_dmy(message.text)
    except ValueError:
        await message.answer("Формат: ДД-ММ-ГГГГ")
        return
    await state.update_data(once_date=on_date.strftime("%d-%m-%Y"))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перенести", callback_data="once_move")],
            [InlineKeyboardButton(text="Отменить занятие", callback_data="once_cancel")],
        ]
    )
    await message.answer(f"Занятие {format_school_date(on_date)}:", reply_markup=kb)


@router.callback_query(F.data == "once_cancel")
async def cb_once_cancel(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if query.from_user is None or "slot_id" not in data or "once_date" not in data:
        await query.answer("Начните заново")
        return
    on_date = parse_dmy(data["once_date"])
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, query.from_user.id)
        if admin is None or not _is_admin(admin):
            await query.answer(REFUSAL, show_alert=True)
            return
        await cancel_occurrence(session, data["slot_id"], on_date)
        await session.commit()
    await state.clear()
    await query.answer("Отменено")
    if query.message:
        await query.message.answer(
            f"Занятие {format_school_date(on_date)} отменено. Остальные недели без изменений."
        )


@router.callback_query(F.data == "once_move")
async def cb_once_move(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeekFSM.time_str)
    await query.answer()
    if query.message:
        await query.message.answer("Новое время начала (ЧЧ:ММ):")


@router.message(WeekFSM.time_str)
async def fsm_week_time(message: Message, state: FSMContext) -> None:
    if not message.text or _parse_hhmm(message.text) is None:
        await message.answer("Формат: ЧЧ:ММ")
        return
    await state.update_data(slot_time=message.text.strip())
    await state.set_state(WeekFSM.duration)
    await message.answer("Длительность в минутах:")


@router.message(WeekFSM.duration)
async def fsm_week_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await message.answer("Введите число минут")
        return
    if minutes <= 0:
        await message.answer("Длительность должна быть больше нуля")
        return
    await state.update_data(slot_duration=str(minutes))
    await state.set_state(WeekFSM.place)
    await message.answer("Место:")


@router.message(WeekFSM.place)
async def fsm_week_place(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(slot_place=message.text.strip())
    await state.set_state(WeekFSM.notes)
    await message.answer("Что взять с собой (или «—»):")


@router.message(WeekFSM.notes)
async def fsm_week_notes(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    notes = (message.text or "—").strip()
    if notes == "—":
        notes = ""
    hour, minute = _parse_hhmm(data["slot_time"]) or (0, 0)
    start = datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time()
    end_dt = datetime.combine(date.today(), start) + timedelta(
        minutes=int(data["slot_duration"])
    )
    end = end_dt.time()
    if end <= start:
        await message.answer("Занятие переходит через полночь — укажите более короткую длительность.")
        await state.set_state(WeekFSM.duration)
        return
    mode = data.get("mode", "new")
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        if mode == "edit_one":
            on_date = parse_dmy(data["once_date"])
            await override_occurrence(
                session, data["slot_id"], on_date, start, end, data["slot_place"], notes or None
            )
            text = f"Изменено только {format_school_date(on_date)}."
        elif mode == "edit_all":
            await update_weekly_slot(
                session,
                data["slot_id"],
                int(data["weekday"]),
                start,
                end,
                data["slot_place"],
                notes or None,
            )
            text = "График изменён на все будущие недели. Уже изменённые отдельные даты не трогал."
        else:
            await create_weekly_slot(
                session,
                data["group_id"],
                int(data["weekday"]),
                start,
                end,
                data["slot_place"],
                notes or None,
            )
            text = "Недельный слот создан. Отдельные даты вводить не нужно."
        await session.commit()
    await message.answer(text)
    await state.clear()


@router.message(SubProductFSM.name)
async def fsm_sub_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(sub_name=message.text.strip())
    await state.set_state(SubProductFSM.lessons)
    await message.answer("Число занятий:")


@router.message(SubProductFSM.lessons)
async def fsm_sub_lessons(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число")
        return
    await state.update_data(sub_lessons=message.text.strip())
    await state.set_state(SubProductFSM.validity)
    await message.answer("Срок действия (дней):")


@router.message(SubProductFSM.validity)
async def fsm_sub_validity(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число")
        return
    await state.update_data(sub_validity=message.text.strip())
    await state.set_state(SubProductFSM.price)
    await message.answer("Цена:")


@router.message(SubProductFSM.price)
async def fsm_sub_price(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    try:
        amount = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Некорректная сумма")
        return
    data = await state.get_data()
    today = _today_local()
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        product = await create_product(
            session,
            "subscription",
            data["sub_name"],
            lessons_count=int(data["sub_lessons"]),
            validity_days=int(data["sub_validity"]),
        )
        await set_price(session, product.id, amount, today)
        await session.commit()
    await message.answer("Абонемент и цена созданы.")
    await state.clear()


@router.message(DropInProductFSM.name)
async def fsm_dropin_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(drop_name=message.text.strip())
    await state.set_state(DropInProductFSM.price)
    await message.answer("Цена:")


@router.message(DropInProductFSM.price)
async def fsm_dropin_price(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    try:
        amount = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Некорректная сумма")
        return
    data = await state.get_data()
    today = _today_local()
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        product = await create_product(session, "drop_in", data["drop_name"])
        await set_price(session, product.id, amount, today)
        await session.commit()
    await message.answer("Разовое занятие создано.")
    await state.clear()


@router.message(NewPriceFSM.amount)
async def fsm_new_price_amount(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Некорректная сумма")
        return
    await state.update_data(price_amount=message.text.strip())
    await state.set_state(NewPriceFSM.valid_from)
    await message.answer("Дата начала цены (ДД-ММ-ГГГГ):")


@router.message(NewPriceFSM.valid_from)
async def fsm_new_price_valid_from(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    try:
        valid_from = parse_dmy(message.text.strip())
    except ValueError:
        await message.answer("Формат: ДД-ММ-ГГГГ")
        return
    data = await state.get_data()
    amount = Decimal(data["price_amount"].replace(",", "."))
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        await set_price(session, data["product_id"], amount, valid_from)
        await session.commit()
    await message.answer("Цена сохранена.")
    await state.clear()


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


async def send_due_reminder(bot: Bot, item: DueReminder) -> None:
    """Send a planned reminder; inline and reply keyboards are attached per Telegram rules."""
    if item.telegram_user_id is None:
        return
    inline = reminder_inline_markup(item)
    reply_kb = reminder_reply_keyboard(item)
    chat_id = item.telegram_user_id
    sid = item.school_session_id
    if inline is not None:
        token = bind_outbound_school_session_id(sid)
        try:
            await bot.send_message(chat_id, item.text, reply_markup=inline)
        finally:
            reset_outbound_context(token)
        if reply_kb is not None:
            token = bind_outbound_school_session_id(None)
            try:
                await bot.send_message(chat_id, "\u2060", reply_markup=reply_kb)
            finally:
                reset_outbound_context(token)
    elif reply_kb is not None:
        token = bind_outbound_school_session_id(sid)
        try:
            await bot.send_message(chat_id, item.text, reply_markup=reply_kb)
        finally:
            reset_outbound_context(token)
    else:
        token = bind_outbound_school_session_id(sid)
        try:
            await bot.send_message(chat_id, item.text)
        finally:
            reset_outbound_context(token)
