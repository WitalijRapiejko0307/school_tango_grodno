"""Telegram bot handlers (aiogram 3) — UX variant A."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .states import EnrollFSM, ListPickFSM
from app.db import async_session_maker
from app.models import Attendance, Person, Product, SchoolSession, Subscription
from app.services.billing import (
    person_has_unpaid_started,
    subscription_payment_status,
)
from app.services.identity import (
    has_pending_phone_invite,
    upsert_from_telegram,
)
from app.services.bot_messages import (
    bind_outbound_school_session_id,
    reset_outbound_context,
)
from app.services.notifications import (
    DueReminder,
    answer_attendance,
    find_today_own_session_conflict,
    guest_rsvp,
    record_coming,
    record_not_coming,
    second_booking_prompt_text,
)
from app.services.school_time import (
    format_school_date,
    format_school_datetime,
    format_school_time,
    school_tz,
)
from app.services.roster import (
    admin_contacts,
    claim_person,
    find_unclaimed_by_query,
)
from app.services.pick_list import (
    HINT_INDEX,
    PickError,
    PickRow,
    page_text,
    resolve_picks,
)
from app.services.schedule import active_group_membership, materialize_range

logger = logging.getLogger(__name__)

router = Router(name="telegram.core")

from .people import (  # noqa: E402
    _active_membership,
    _format_admin_contacts_list,
)
from .common import (  # noqa: E402
    ARCHIVED_ACCESS_CLOSED,
    BTN_CLIENT_CLASSES,
    BTN_CLIENT_SUBS,
    BTN_TODAY,
    REFUSAL,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _ensure_person,
    _guest_keyboard_with_phone,
    _is_admin,
    _person_by_telegram,
    _require_admin_callback,
    _require_admin_message_strict,
    _get_pick_spec,
    _list_pick_hint,
    _list_pick_keyboard,
    _list_pick_more_callback,
    _list_pick_rows_from_data,
    _start_list_pick,
    _today_local,
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
)
_REEXPORT_FROM_COMMON = (
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
)

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())









_WEEKDAY_FULL_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_UNPAID_SUBSCRIPTION_NOTE = (
    "Есть неоплаченный абонемент — его можно закрыть у администратора."
)
_CLIENT_SUBS_PAGE = 3


def _format_class_date(day: date) -> str:
    return f"{_WEEKDAY_FULL_RU[day.weekday()]}, {format_school_date(day)}"


def _session_calendar_day(school_session: SchoolSession) -> date:
    if school_session.session_date is not None:
        return school_session.session_date
    starts = school_session.starts_at
    if starts.tzinfo is None:
        return starts.replace(tzinfo=school_tz()).date()
    return starts.astimezone(school_tz()).date()


def _format_class_session_line(school_session: SchoolSession) -> str:
    day = _session_calendar_day(school_session)
    return (
        f"{_format_class_date(day)}, "
        f"{format_school_time(school_session.starts_at)}, "
        f"{school_session.place}"
    )


def _subscription_period_label(sub: Subscription) -> str:
    if sub.starts_on is None:
        if sub.valid_until is not None:
            return f"с первого визита — {format_school_date(sub.valid_until)}"
        return "с первого визита"
    start = format_school_date(sub.starts_on)
    if sub.valid_until is not None:
        return f"{start}–{format_school_date(sub.valid_until)}"
    return start


def _subscription_is_inactive(sub: Subscription, today: date) -> bool:
    if sub.lessons_left == 0:
        return True
    if sub.valid_until is not None and sub.valid_until < today:
        return True
    if sub.starts_on is not None and sub.starts_on > today:
        return True
    return False


def _subscription_covers_day(sub: Subscription, day: date) -> bool:
    if sub.starts_on is not None and day < sub.starts_on:
        return False
    if sub.valid_until is not None and day > sub.valid_until:
        return False
    return True


async def _person_subscriptions(
    session: AsyncSession, person_id: str
) -> list[Subscription]:
    result = await session.execute(
        select(Subscription)
        .where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            )
        )
        .order_by(desc(Subscription.created_at))
    )
    return list(result.scalars().all())


async def _visit_lines_for_subscription(
    session: AsyncSession, person_id: str, sub: Subscription
) -> list[str]:
    result = await session.execute(
        select(SchoolSession)
        .join(Attendance, Attendance.session_id == SchoolSession.id)
        .where(Attendance.person_id == person_id)
        .order_by(SchoolSession.starts_at)
    )
    lines: list[str] = []
    for school_session in result.scalars().all():
        day = _session_calendar_day(school_session)
        if _subscription_covers_day(sub, day):
            lines.append(_format_class_date(day))
    return lines


async def _format_client_subscription_block(
    session: AsyncSession,
    person_id: str,
    sub: Subscription,
    product: Product | None,
    today: date,
) -> str:
    name = product.name if product is not None else "—"
    status = "неактивен" if _subscription_is_inactive(sub, today) else "активен"
    period = _subscription_period_label(sub)
    header = f"{name}\n{status}; осталось {sub.lessons_left}; {period}"
    if subscription_payment_status(sub) == "unpaid":
        header += "\nне оплачен"
    visits = await _visit_lines_for_subscription(session, person_id, sub)
    if visits:
        header += "\n" + "\n".join(visits)
    return header


async def _client_subscriptions_text(
    session: AsyncSession,
    person_id: str,
    *,
    offset: int = 0,
    limit: int | None = _CLIENT_SUBS_PAGE,
) -> str:
    subs = await _person_subscriptions(session, person_id)
    if not subs:
        return "Абонементов пока нет."
    today = _today_local()
    slice_end = len(subs) if limit is None else min(len(subs), offset + limit)
    blocks: list[str] = []
    for sub in subs[offset:slice_end]:
        product = await session.get(Product, sub.product_id)
        blocks.append(
            await _format_client_subscription_block(
                session, person_id, sub, product, today
            )
        )
    return "\n\n".join(blocks)


async def _append_unpaid_note_if_needed(
    session: AsyncSession, person_id: str, text: str
) -> str:
    if await person_has_unpaid_started(session, person_id, _today_local()):
        return f"{text}\n{_UNPAID_SUBSCRIPTION_NOTE}"
    return text


async def _group_and_next_session_message(
    session: AsyncSession, person_id: str
) -> str:
    from .groups import _session_group_name

    membership = await _active_membership(session, person_id)
    if membership is None:
        return "Вы записаны на занятия. Расписание появится, когда назначат занятия."
    gname = await _session_group_name(session, membership.group_id)
    today = _today_local()
    await materialize_range(session, today, today + timedelta(days=21))
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
        return (
            f"Вы в «{gname}».\n"
            f"Ближайшее: {_format_class_session_line(nxt)}"
        )
    return f"Вы в «{gname}». Ближайших занятий пока нет."


async def _notify_admins_claim(
    bot: Bot, session: AsyncSession, claimed_full_name: str, telegram_user_id: int
) -> None:
    result = await session.execute(
        select(Person).where(
            Person.role == "admin",
            Person.telegram_user_id.is_not(None),
        )
    )
    for admin in result.scalars().all():
        if admin.telegram_user_id == telegram_user_id:
            continue
        try:
            await bot.send_message(
                admin.telegram_user_id,
                f"Пользователь Telegram {telegram_user_id} привязался к карточке «{claimed_full_name}».",
            )
        except Exception:
            logger.exception(
                "Failed to notify admin %s about roster claim",
                admin.telegram_user_id,
            )


async def _enroll_claim_by_id(
    bot: Bot,
    telegram_user_id: int,
    username: str | None,
    telegram_full_name: str,
    person_id: str,
    state: FSMContext,
    reply_target: Message,
) -> None:
    async with async_session_maker() as session:
        guest = await _person_by_telegram(session, telegram_user_id)
        try:
            if guest is not None and guest.id != person_id:
                from app.services.roster import delete_guest_shell

                await delete_guest_shell(session, guest)
            person = await claim_person(
                session,
                person_id,
                telegram_user_id=telegram_user_id,
                username=username,
                telegram_full_name=telegram_full_name,
            )
            text = await _group_and_next_session_message(session, person.id)
            await _notify_admins_claim(bot, session, person.full_name, telegram_user_id)
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            if "telegram_already_linked" in str(exc):
                await reply_target.answer(
                    "Эта карточка уже привязана к другому Telegram."
                )
                return
            raise
        except Exception:
            await session.rollback()
            logger.exception("Roster claim failed for telegram %s", telegram_user_id)
            await reply_target.answer(
                "Не получилось привязать карточку. Напишите фамилию и имя ещё раз."
            )
            return
    await state.clear()
    await reply_target.answer(text, reply_markup=reply_markup_for_role("client"))


# --- /start ---


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    person = await _ensure_person(message)
    if person is None:
        return
    if person.archived_at is not None and person.role != "admin":
        await message.answer(ARCHIVED_ACCESS_CLOSED)
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
        enroll_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data="enroll_yes"),
                    InlineKeyboardButton(text="Нет", callback_data="enroll_no"),
                ]
            ]
        )
        await message.answer(
            "Вы уже записаны на занятия или знаете, когда у вас занятие?",
            reply_markup=enroll_kb,
        )
        return
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "enroll_no")
async def cb_enroll_no(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(
            "Хорошо. Откройте «Расписание» в меню ниже, чтобы выбрать занятие.",
            reply_markup=reply_markup_for_role("guest"),
        )


@router.callback_query(F.data == "enroll_yes")
async def cb_enroll_yes(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user is None:
        return
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None or person.role != "guest":
            await query.answer("Недоступно", show_alert=True)
            return
    await state.set_state(EnrollFSM.waiting_name)
    await query.answer()
    if query.message:
        await query.message.answer(
            "Напишите одной строкой фамилию и имя — в любом порядке."
        )


@router.callback_query(F.data.startswith("claim_pick:"))
async def cb_claim_pick(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.from_user is None:
        return
    person_id = query.data.split(":", 1)[1]
    if query.message is None:
        return
    await _enroll_claim_by_id(
        query.bot,
        query.from_user.id,
        query.from_user.username,
        query.from_user.full_name or "User",
        person_id,
        state,
        query.message,
    )
    await query.answer()


@router.message(EnrollFSM.waiting_name)
async def fsm_enroll_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    query_text = message.text.strip()
    if not query_text:
        return
    async with async_session_maker() as session:
        matches = await find_unclaimed_by_query(session, query_text)
        if not matches:
            contacts = await admin_contacts(session)
            await session.commit()
            if contacts:
                await message.answer(_format_admin_contacts_list(contacts))
            else:
                await message.answer(
                    "Совпадений нет. Обратитесь к администратору школы."
                )
            await state.clear()
            return
        if len(matches) == 1:
            await session.commit()
            await _enroll_claim_by_id(
                message.bot,
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name or "User",
                matches[0].id,
                state,
                message,
            )
            return
        buttons: list[list[InlineKeyboardButton]] = []
        from .groups import _session_group_name

        for person in matches:
            membership = await _active_membership(session, person.id)
            gname = "—"
            if membership is not None:
                gname = await _session_group_name(session, membership.group_id)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{person.full_name} ({gname})",
                        callback_data=f"claim_pick:{person.id}",
                    )
                ]
            )
        await session.commit()
    await message.answer(
        "Найдено несколько совпадений. Выберите себя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


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
        reply = await _append_unpaid_note_if_needed(
            session, person.id, "записали, что идёте"
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(reply)


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
        reply = await _append_unpaid_note_if_needed(
            session, person.id, "приняли, что не идёте"
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(reply)


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
        text = await _append_unpaid_note_if_needed(session, person.id, text)
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


async def _start_admin_attendance_pick(
    message: Message,
    state: FSMContext,
    school_session_id: str,
) -> None:
    from .groups import _attendance_pick_rows, _members_with_attendance

    async with async_session_maker() as session:
        school_session = await session.get(SchoolSession, school_session_id)
        if school_session is None:
            await message.answer("Не найдено")
            return
        members = await _members_with_attendance(
            session, school_session.group_id, school_session_id
        )
        rows = _attendance_pick_rows(members)
        title = "У выбранных номеров переключится отметка"
        purpose = f"att:{school_session_id}"
    await _start_list_pick(
        message,
        state,
        purpose=purpose,
        rows=rows,
        title=title,
    )


@router.callback_query(F.data.startswith("adm_sess:"))
async def cb_admin_session(
    query: CallbackQuery, state: FSMContext
) -> None:
    if query.data is None:
        return
    school_session_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await query.answer()
    if query.message:
        await _start_admin_attendance_pick(
            query.message, state, school_session_id
        )


async def _complete_other_group_visit(
    session: AsyncSession, person_id: str, target_session_id: str, now: datetime
) -> str:
    from .groups import _client_mark_yes

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





# pp_more (people list) has its own handler in people.py.
@router.callback_query(F.data.endswith("_more") & (F.data != "pp_more"))
async def cb_list_pick_more(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    current = await state.get_state()
    if current != ListPickFSM.waiting.state:
        await query.answer("Список устарел")
        return
    data = await state.get_data()
    purpose = str(data.get("pick_purpose") or "")
    expected = _list_pick_more_callback(purpose)
    if query.data != expected:
        await query.answer("Список устарел")
        return
    page = int(data.get("pick_page") or 0) + 1
    async with async_session_maker() as session:
        rows = await _list_pick_rows_from_data(session, data)
    if not rows or page >= (len(rows) + 19) // 20:
        await query.answer("Больше нет")
        return
    await state.update_data(pick_page=page)
    await query.answer()
    if query.message:
        await query.message.answer(
            page_text(rows, page, _list_pick_hint(purpose)),
            reply_markup=_list_pick_keyboard(rows, page, purpose),
        )


@router.callback_query(F.data == "pick_cancel")
async def cb_list_pick_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer("Выбор отменён.")




# --- Reply menu handlers ---


@router.message(F.text == BTN_TODAY)
async def menu_today(message: Message, state: FSMContext) -> None:
    from .groups import _list_today_sessions, _session_group_name

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
        rows: list[PickRow] = []
        for s in sessions:
            gname = await _session_group_name(session, s.group_id)
            detail = f"{format_school_datetime(s.starts_at)}, {s.place}"
            rows.append(PickRow(id=s.id, full_name=gname, detail=detail))
    await _start_list_pick(
        message,
        state,
        purpose="today",
        rows=rows,
        title="Номер откроет отметку занятия.",
        hint=HINT_INDEX,
    )




@router.message(F.text == BTN_CLIENT_SUBS)
async def menu_client_subscriptions(message: Message) -> None:
    if message.from_user is None:
        return
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)
        if person is None or person.role != "client":
            return
        subs = await _person_subscriptions(session, person.id)
        text = await _client_subscriptions_text(session, person.id)
        markup = None
        if len(subs) > _CLIENT_SUBS_PAGE:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Показать ещё",
                            callback_data=f"client_subs_more:{person.id}",
                        )
                    ]
                ]
            )
        await session.commit()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("client_subs_more:"))
async def cb_client_subs_more(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None:
        return
    person_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, query.from_user.id)
        if person is None or person.id != person_id or person.role != "client":
            await query.answer("Недоступно", show_alert=True)
            return
        text = await _client_subscriptions_text(
            session, person.id, offset=_CLIENT_SUBS_PAGE, limit=None
        )
        await session.commit()
    await query.answer()
    if query.message:
        await query.message.answer(text)


@router.message(F.text == BTN_CLIENT_CLASSES)
async def menu_client_classes(message: Message) -> None:
    if message.from_user is None:
        return
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)
        if person is None or person.role != "client":
            return
        membership = await active_group_membership(session, person.id)
        if membership is None:
            await session.commit()
            await message.answer("Вы не в группе.")
            return
        today = _today_local()
        end = today + timedelta(days=7)
        await materialize_range(session, today, end)
        tz = school_tz()
        range_start = datetime(today.year, today.month, today.day, tzinfo=tz)
        range_end = datetime(end.year, end.month, end.day, tzinfo=tz)
        start_utc = range_start.astimezone(UTC)
        end_utc = range_end.astimezone(UTC)
        result = await session.execute(
            select(SchoolSession)
            .where(
                SchoolSession.group_id == membership.group_id,
                SchoolSession.starts_at >= start_utc,
                SchoolSession.starts_at < end_utc,
                SchoolSession.status == "scheduled",
            )
            .order_by(SchoolSession.starts_at)
        )
        sessions = list(result.scalars().all())
        await session.commit()
    if not sessions:
        await message.answer("На ближайшую неделю занятий нет.")
        return
    lines = [_format_class_session_line(s) for s in sessions]
    await message.answer("\n".join(lines))




# --- FSM text steps ---


@router.message(ListPickFSM.waiting, F.text)
async def fsm_list_pick_waiting(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
            return
    if message.text.strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Выбор отменён.")
        return
    data = await state.get_data()
    purpose = str(data.get("pick_purpose") or "")
    async with async_session_maker() as session:
        rows = await _list_pick_rows_from_data(session, data)
    if not rows:
        await state.clear()
        await message.answer("Список устарел.")
        return

    spec = _get_pick_spec(purpose)
    if spec is not None and spec.is_index_pick:
        if purpose == "ppl_add" and not message.text.strip().isdigit():
            await message.answer(HINT_INDEX)
            return
        try:
            picked = resolve_picks(rows, message.text, many=False)
        except PickError as exc:
            await message.answer(exc.message)
            return
        if spec.on_index_pick is not None:
            await spec.on_index_pick(message, state, picked)
        return

    try:
        picked = resolve_picks(rows, message.text, many=True)
    except PickError as exc:
        await message.answer(exc.message)
        return

    if spec is not None and spec.on_many_pick is not None:
        await spec.on_many_pick(message, state, picked, admin)








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
