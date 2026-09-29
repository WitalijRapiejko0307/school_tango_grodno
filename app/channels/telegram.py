"""Telegram bot handlers (aiogram 3) — UX variant A."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F, Router
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import async_session_maker
from app.models import (
    Attendance,
    ContactCard,
    Group,
    GroupMembership,
    Person,
    Product,
    Reminder,
    SchoolSession,
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
from app.services.notifications import (
    DueReminder,
    answer_attendance,
    guest_day_summary,
    guest_rsvp,
    list_reminders,
    record_coming,
)
from app.services.schedule import (
    add_stream_member,
    assign_group,
    create_group,
    create_session,
    create_stream,
    list_month_sessions,
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


def _reply_markup_for_role(role: str) -> ReplyKeyboardMarkup:
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


def _school_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().SCHOOL_TZ)


def _today_local() -> date:
    return datetime.now(_school_tz()).date()


def _format_session_local(starts_at: datetime) -> str:
    local = starts_at.astimezone(_school_tz()) if starts_at.tzinfo else starts_at
    return local.strftime("%d.%m %H:%M")


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
    result = await session.execute(
        select(GroupMembership).where(
            GroupMembership.person_id == person_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    return result.scalar_one_or_none()


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


class SlotFSM(StatesGroup):
    group_id = State()
    date_str = State()
    time_str = State()
    duration = State()
    place = State()
    bring_notes = State()


class StreamFSM(StatesGroup):
    name = State()
    username = State()


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
    markup = _reply_markup_for_role(person.role)
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
            reply_markup=_reply_markup_for_role("admin"),
        )
        return
    await message.answer(
        "Этот номер не найден среди приглашений администраторов.",
        reply_markup=_reply_markup_for_role("guest"),
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
            members, group_name, _format_session_local(school_session.starts_at)
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
            members, group_name, _format_session_local(school_session.starts_at)
        )
        kb = _attendance_keyboard(school_session_id, members)
    await query.answer()
    if query.message:
        await query.message.edit_text(text, reply_markup=kb)


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
        text = await _client_mark_yes(
            session, person.id, session_id, datetime.now(UTC)
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


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
    await state.set_state(SlotFSM.date_str)
    await state.update_data(group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer("Дата занятия (ГГГГ-ММ-ДД):")


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
                reply_markup=_reply_markup_for_role("client"),
            )
        except Exception:
            logger.exception("Failed to refresh client menu for %s", notify_id)


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


@router.callback_query(F.data == "people_stream")
async def cb_people_stream(query: CallbackQuery, state: FSMContext) -> None:
    admin = await _person_for_callback(query)
    if admin is None or not _is_admin(admin):
        await query.answer(REFUSAL, show_alert=True)
        return
    await state.set_state(StreamFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название потока:")


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
        if not sessions:
            await message.answer("На сегодня занятий нет.")
            return
        lines = ["Занятия сегодня:"]
        buttons = []
        for s in sessions:
            gname = await _session_group_name(session, s.group_id)
            lines.append(f"• {gname} — {_format_session_local(s.starts_at)}, {s.place}")
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
            [InlineKeyboardButton(text="Слот", callback_data="admin_slot")],
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
            [InlineKeyboardButton(text="Поток", callback_data="people_stream")],
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
        reminders = await list_reminders(session, limit=15)
        if not reminders:
            await message.answer("Напоминаний пока нет.")
            return
        lines = []
        for r in reminders:
            sent = r.sent_at.isoformat() if r.sent_at else "—"
            lines.append(f"{r.kind} | {sent} | ответ: {r.response}")
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
                f"{_format_session_local(school_session.starts_at)}, {school_session.place}"
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
                f"Группа: {gname}\nБлижайшее: {_format_session_local(nxt.starts_at)}, {nxt.place}"
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
                f"Осталось занятий: {sub.lessons_left}, действует до {sub.valid_until}"
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
                        text=f"{gname} {_format_session_local(s.starts_at)}",
                        callback_data=f"other_sess:{s.id}",
                    )
                ]
            )
        await message.answer(
            "Выберите занятие:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.message(F.text == BTN_SCHEDULE)
async def menu_schedule(message: Message) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None:
            return
        now_local = datetime.now(_school_tz())
        sessions = await list_month_sessions(
            session, now_local.year, now_local.month, get_settings().SCHOOL_TZ
        )
        if not sessions:
            contacts = await _format_contacts(session)
            await message.answer(
                "В этом месяце открытых занятий нет.\n\n" + contacts
            )
            return
        lines = ["Расписание:"]
        buttons = []
        for s in sessions:
            gname = await _session_group_name(session, s.group_id)
            local_t = _format_session_local(s.starts_at)
            lines.append(f"• {local_t} — {gname}, {s.place}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"Буду {local_t}",
                        callback_data=f"rsvp:{s.id}",
                    )
                ]
            )
        await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


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
        group = await create_group(session, message.text.strip())
        await session.commit()
        await message.answer(f"Группа «{group.name}» создана.")
    await state.clear()


@router.message(SlotFSM.date_str)
async def fsm_slot_date(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("Формат: ГГГГ-ММ-ДД")
        return
    await state.update_data(slot_date=message.text.strip())
    await state.set_state(SlotFSM.time_str)
    await message.answer("Время начала (ЧЧ:ММ):")


@router.message(SlotFSM.time_str)
async def fsm_slot_time(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if not re.match(r"^\d{1,2}:\d{2}$", message.text.strip()):
        await message.answer("Формат: ЧЧ:ММ")
        return
    await state.update_data(slot_time=message.text.strip())
    await state.set_state(SlotFSM.duration)
    await message.answer("Длительность в минутах:")


@router.message(SlotFSM.duration)
async def fsm_slot_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        int(message.text.strip())
    except ValueError:
        await message.answer("Введите число минут")
        return
    await state.update_data(slot_duration=message.text.strip())
    await state.set_state(SlotFSM.place)
    await message.answer("Место:")


@router.message(SlotFSM.place)
async def fsm_slot_place(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(slot_place=message.text.strip())
    await state.set_state(SlotFSM.bring_notes)
    await message.answer("Что взять с собой (текст для гостей):")


@router.message(SlotFSM.bring_notes)
async def fsm_slot_notes(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    notes = (message.text or "—").strip()
    tz = _school_tz()
    d = date.fromisoformat(data["slot_date"])
    h, m = map(int, data["slot_time"].split(":"))
    duration = int(data["slot_duration"])
    starts_local = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    ends_local = starts_local + timedelta(minutes=duration)
    starts_utc = starts_local.astimezone(UTC)
    ends_utc = ends_local.astimezone(UTC)
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        await create_session(
            session,
            data["group_id"],
            starts_utc,
            ends_utc,
            data["slot_place"],
            notes,
        )
        await session.commit()
    await message.answer("Слот создан.")
    await state.clear()


@router.message(StreamFSM.name)
async def fsm_stream_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(stream_name=message.text.strip())
    await state.set_state(StreamFSM.username)
    await message.answer("Username человека (@ник) для добавления в поток:")


@router.message(StreamFSM.username)
async def fsm_stream_username(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    norm = message.text.strip().lstrip("@").lower()
    data = await state.get_data()
    async with async_session_maker() as session:
        admin = await _person_by_telegram(session, message.from_user.id)
        if admin is None or not _is_admin(admin):
            await message.answer(REFUSAL)
            await state.clear()
            return
        stream = await create_stream(session, data["stream_name"])
        result = await session.execute(
            select(Person).where(
                func.lower(func.ltrim(Person.username, "@")) == norm
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            await message.answer("Человек с таким username не найден. Поток создан без участника.")
        else:
            await add_stream_member(session, stream.id, target.id)
            await message.answer(f"В поток добавлен: {target.full_name}")
        await session.commit()
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
    await message.answer("Дата начала цены (ГГГГ-ММ-ДД):")


@router.message(NewPriceFSM.valid_from)
async def fsm_new_price_valid_from(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    try:
        valid_from = date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("Формат: ГГГГ-ММ-ДД")
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
    if item.kind == "session_start" and item.school_session_id:
        sid = item.school_session_id
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Иду", callback_data=f"go:{sid}")]
            ]
        )
    return None
