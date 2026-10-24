"""Telegram bot handlers (aiogram 3) — UX variant A."""

from __future__ import annotations

import logging
import re
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
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .states import (
    AdminMenuFSM,
    AssignGroupFSM,
    EnrollFSM,
    GroupRenameFSM,
    GroupStreamFSM,
    GuestPickFSM,
    ListPickFSM,
    NewGroupFSM,
    PeoplePickFSM,
    RosterFSM,
    SlotFSM,
    WeekFSM,
)

# Referenced only for `from .legacy import *` (no handlers in this module yet).
_REEXPORT_ONLY_FSM = (AdminMenuFSM, AssignGroupFSM, SlotFSM)
from app.db import async_session_maker
from app.models import (
    Attendance,
    WeeklySlot,
    Group,
    GroupMembership,
    Person,
    Reminder,
    SchoolSession,
    Stream,
)
from app.services.attendance import mark_attendance
from app.services.billing import active_subscription
from app.services.identity import (
    has_pending_phone_invite,
    upsert_from_telegram,
)
from app.services.bot_messages import (
    bind_outbound_school_session_id,
    reset_outbound_context,
)
from app.services.notices import (
    cancel_notice_text,
    group_audience,
    notice_audience,
    reschedule_notice_text,
    upcoming_sessions,
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
from app.services.school_time import format_school_date, format_school_datetime
from app.services.ocr import extract_roster_lines
from app.services.roster import (
    RosterDraftItem,
    admin_contacts,
    claim_person,
    confirm_roster,
    find_unclaimed_by_query,
    parse_roster_line,
)
from app.services.pick_list import (
    HINT_INDEX,
    PickError,
    PickRow,
    attendance_report,
    page_text,
    resolve_picks,
    short_report,
)
from app.services.schedule import (
    WEEKDAY_LABELS,
    archive_group,
    assign_group,
    cancel_occurrence,
    count_archive_impact,
    count_slot_deactivate_impact,
    create_group,
    create_stream,
    create_weekly_slot,
    deactivate_weekly_slot,
    ensure_occurrence,
    group_transfer_confirmation_text,
    materialize_range,
    override_occurrence,
    parse_dmy,
    rename_group,
    restore_group,
    restore_weekly_slot,
    set_group_stream,
    update_weekly_slot,
)

logger = logging.getLogger(__name__)

router = Router(name="telegram")

from .billing import (  # noqa: E402
    _prompt_open_sub_products,
    _report_open_subscription,
)
from .people import (  # noqa: E402
    _active_membership,
    _format_admin_contacts_list,
    _format_contacts,
    _list_non_admin_pick_rows,
    _prompt_leave_person,
    _send_person_card,
    _start_people_pick,
)
from .common import (  # noqa: E402
    ARCHIVED_ACCESS_CLOSED,
    BTN_BALANCE,
    BTN_GROUPS,
    BTN_MY_GROUP,
    BTN_OTHER_GROUP,
    BTN_SCHEDULE,
    BTN_TODAY,
    REFUSAL,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _ensure_person,
    _guest_keyboard_with_phone,
    _is_admin,
    _parse_hhmm,
    _person_by_telegram,
    _require_admin_callback,
    _require_admin_message,
    _require_admin_message_strict,
    _school_tz,
    _get_pick_spec,
    _list_pick_hint,
    _list_pick_keyboard,
    _list_pick_more_callback,
    _list_pick_rows_from_data,
    _pick_ids_from_data,
    _send_notice,
    _start_list_pick,
    _today_local,
    _weekday_keyboard,
    PickSpec,
    register_att_pick_spec,
    register_pick_spec,
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
            f"Ближайшее: {format_school_datetime(nxt.starts_at)}, {nxt.place}"
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
                guest.telegram_user_id = None
                guest.username = None
                await session.flush()
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





@router.callback_query(
    F.data.in_(
        (
            "att_more",
            "msg_more",
            "grp_more",
            "prd_more",
            "today_more",
            "sess_more",
            "slot_week_more",
            "slot_once_more",
            "grp_slot_more",
            "grp_roster_more",
            "grp_stream_more",
            "grp_assign_more",
            "ppl_add_more",
        )
    )
)
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
                (
                    f"Осталось занятий: {sub.lessons_left}. "
                    + (
                        f"Действует до {format_school_date(sub.valid_until)}."
                        if sub.valid_until is not None
                        else "Срок начнётся с первого посещения."
                    )
                )
            )


@router.message(F.text == BTN_OTHER_GROUP)
async def menu_other_group(message: Message) -> None:
    from .groups import _list_today_sessions, _session_group_name

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






# --- FSM text steps ---


@router.message(ListPickFSM.waiting, F.text)
async def fsm_list_pick_waiting(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
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
