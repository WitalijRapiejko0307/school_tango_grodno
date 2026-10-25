"""Telegram notices/messaging: broadcasts, reminders follow-up, guest messaging."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import DayGather, GuestRsvp, Person, SchoolSession
from app.services.notices import (
    cancel_notice_text,
    cancel_session,
    free_notice_text,
    group_audience,
    notice_audience,
    reschedule_notice_text,
    reschedule_session,
)
from app.services.gather import list_today_gather_summary
from app.services.notifications import (
    guest_day_summary,
    guest_rsvp,
)
from app.services.pick_list import HINT_MANY, PickRow, page_text, short_report
from app.services.school_time import format_school_datetime

from .common import (
    BTN_NOTICE,
    BTN_REMINDERS,
    PickSpec,
    REFUSAL,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _admin_person,
    _get_pick_spec,
    _list_pick_keyboard,
    _parse_school_slot,
    _person_by_telegram,
    _pick_ids_from_data,
    _require_admin_callback,
    _require_admin_message,
    _require_admin_message_strict,
    _send_notice,
    _start_list_pick,
    _today_local,
    register_pick_spec,
)
from .states import GuestPickFSM, ListPickFSM, NoticeFSM

router = Router(name="telegram.notices")

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())


async def _build_rows_msg(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    from .people import _pick_rows_for_ids

    return await _pick_rows_for_ids(
        session, _pick_ids_from_data(data), with_group_detail=False
    )


async def _build_rows_guests(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    day_raw = data.get("pick_day")
    if not day_raw:
        return []
    day = date.fromisoformat(str(day_raw))
    guest_rows = await guest_day_summary(session, day)
    by_rsvp = {
        rsvp.id: (rsvp, guest, school_session)
        for rsvp, guest, school_session in guest_rows
    }
    rows: list[PickRow] = []
    for rsvp_id in _pick_ids_from_data(data):
        entry = by_rsvp.get(rsvp_id)
        if entry is None:
            continue
        rsvp, guest, school_session = entry
        phone = guest.phone or "—"
        detail = (
            f"{phone} — {format_school_datetime(school_session.starts_at)}, "
            f"{school_session.place}"
        )
        rows.append(PickRow(id=rsvp.id, full_name=guest.full_name, detail=detail))
    return rows


async def _build_rows_reminders(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    day_raw = data.get("pick_day")
    if not day_raw:
        return []
    day = date.fromisoformat(str(day_raw))
    summary = await list_today_gather_summary(session, day)
    by_rem = {rem.id: (rem, person, detail) for rem, person, detail in summary}
    rows: list[PickRow] = []
    for rem_id in _pick_ids_from_data(data):
        entry = by_rem.get(rem_id)
        if entry is None:
            continue
        rem, person, detail = entry
        rows.append(PickRow(id=rem.id, full_name=person.full_name, detail=detail))
    return rows


async def _notice_recipient_msg(
    session: AsyncSession, row: PickRow
) -> Person | None:
    return await session.get(Person, row.id)


async def _notice_recipient_guests(
    session: AsyncSession, row: PickRow
) -> Person | None:
    rsvp = await session.get(GuestRsvp, row.id)
    if rsvp is None:
        return None
    return await session.get(Person, rsvp.person_id)


async def _notice_recipient_reminders(
    session: AsyncSession, row: PickRow
) -> Person | None:
    rem = await session.get(DayGather, row.id)
    if rem is None:
        return None
    return await session.get(Person, rem.person_id)


async def _on_many_pick_notice(
    message: Message,
    state: FSMContext,
    picked: list[PickRow],
    admin: Person,
) -> None:
    data = await state.get_data()
    purpose = str(data.get("pick_purpose") or "")
    await state.set_state(ListPickFSM.notice_text)
    await state.update_data(
        notice_purpose=purpose,
        notice_pick_ids=[row.id for row in picked],
    )
    await message.answer("Текст сообщения:")


async def _persons_for_notice_pick(
    session: AsyncSession, purpose: str, picked: list[PickRow]
) -> list[Person]:
    spec = _get_pick_spec(purpose)
    if spec is None or spec.notice_recipient is None:
        return []
    seen: set[str] = set()
    people: list[Person] = []
    for row in picked:
        person = await spec.notice_recipient(session, row)
        if person is not None and person.id not in seen:
            seen.add(person.id)
            people.append(person)
    return people


async def _send_notice_session_actions(message: Message, session_id: str) -> None:
    from .groups import _session_group_name

    async with async_session_maker() as session:
        school_session = await session.get(SchoolSession, session_id)
        if school_session is None:
            await message.answer("Не найдено")
            return
        group_name = await _session_group_name(session, school_session.group_id)
        audience = await notice_audience(session, session_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перенести", callback_data=f"ntc_move:{session_id}"
                ),
                InlineKeyboardButton(
                    text="Отменить", callback_data=f"ntc_cancel:{session_id}"
                ),
            ]
        ]
    )
    await message.answer(
        f"{group_name}, {format_school_datetime(school_session.starts_at)}, "
        f"{school_session.place}.\n"
        f"Сообщение уйдёт {len(audience)} чел.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("ntc_sess:"))
async def cb_notice_session(query: CallbackQuery) -> None:
    if query.data is None or query.from_user is None or query.message is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        if await _require_admin_callback(query, session) is None:
            return
        school_session = await session.get(SchoolSession, session_id)
        if school_session is None:
            await query.answer(REFUSAL, show_alert=True)
            return
    await query.answer()
    await _send_notice_session_actions(query.message, session_id)


@router.callback_query(F.data.startswith("ntc_move:"))
async def cb_notice_move(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.from_user is None or query.message is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        if await _require_admin_callback(query, session) is None:
            return
    await state.set_state(NoticeFSM.move_when)
    await state.update_data(session_id=session_id)
    await query.answer()
    await query.message.answer("Новые дата и время: ДД.ММ.ГГГГ ЧЧ:ММ")


@router.message(NoticeFSM.move_when)
async def fsm_notice_when(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    starts = _parse_school_slot(message.text)
    if starts is None:
        await message.answer("Нужен формат ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    await state.update_data(new_starts=starts.isoformat())
    await state.set_state(NoticeFSM.move_place)
    await message.answer("Место. Чтобы оставить прежнее, отправьте «то же».")


@router.message(NoticeFSM.move_place)
async def fsm_notice_place(message: Message, state: FSMContext) -> None:
    from .groups import _session_group_name

    if message.text is None or message.from_user is None or message.bot is None:
        return
    data = await state.get_data()
    session_id = data.get("session_id")
    raw_starts = data.get("new_starts")
    if not session_id or not raw_starts:
        await state.clear()
        await message.answer("Начните снова с «Занятия».")
        return
    new_starts = datetime.fromisoformat(raw_starts)
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        school_session = await session.get(SchoolSession, session_id)
        if school_session is None:
            await message.answer(REFUSAL)
            await state.clear()
            return
        place = (
            school_session.place
            if message.text.strip().lower() == "то же"
            else message.text.strip()
        )
        group_name = await _session_group_name(session, school_session.group_id)
        audience = await notice_audience(session, session_id)
        try:
            updated, old_starts, old_place = await reschedule_session(
                session, session_id, new_starts, place
            )
        except ValueError:
            await message.answer("Это занятие уже нельзя перенести.")
            await state.clear()
            return
        text = reschedule_notice_text(
            group_name, old_starts, old_place, updated.starts_at, updated.place
        )
        await session.commit()
    report, _, _ = await _send_notice(message.bot, audience, text)
    await state.clear()
    await message.answer(f"{text}\n\n{report}")


@router.callback_query(F.data.startswith("ntc_cancel:"))
async def cb_notice_cancel(query: CallbackQuery) -> None:
    from .groups import _session_group_name

    if query.data is None or query.from_user is None or query.message is None or query.bot is None:
        return
    session_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        if await _require_admin_callback(query, session) is None:
            return
        school_session = await session.get(SchoolSession, session_id)
        if school_session is None:
            await query.answer(REFUSAL, show_alert=True)
            return
        group_name = await _session_group_name(session, school_session.group_id)
        audience = await notice_audience(session, session_id)
        text = cancel_notice_text(
            group_name, school_session.starts_at, school_session.place
        )
        try:
            await cancel_session(session, session_id)
        except ValueError:
            await query.answer("Уже отменено", show_alert=True)
            return
        await session.commit()
    report, _, _ = await _send_notice(query.bot, audience, text)
    await query.answer()
    await query.message.answer(f"{text}\n\n{report}")


@router.message(F.text == BTN_NOTICE)
async def menu_notice(message: Message, state: FSMContext) -> None:
    from .groups import _list_active_groups

    if await _admin_person(message) is None:
        await message.answer(REFUSAL)
        return
    await state.clear()
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
        people = list(
            (
                await session.execute(
                    select(Person)
                    .where(Person.telegram_user_id.is_not(None))
                    .order_by(Person.full_name)
                )
            ).scalars().all()
        )
    pick_rows = [PickRow(id=p.id, full_name=p.full_name) for p in people]
    if not groups and not pick_rows:
        await message.answer("Некому отправить: нет групп и людей с Telegram.")
        return
    if groups:
        buttons = [
            [
                InlineKeyboardButton(
                    text=f"Группа: {group.name}",
                    callback_data=f"ntc_grp:{group.id}",
                )
            ]
            for group in groups
        ]
        await message.answer(
            "Кому сообщение:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    if pick_rows:
        await _start_list_pick(
            message,
            state,
            purpose="msg",
            rows=pick_rows,
            title=(
                "Номер или несколько номеров через запятую — кому написать. "
                "Кнопка группы выше отправит сообщение всей группе."
            ),
        )


@router.callback_query(F.data.startswith("ntc_grp:"))
async def cb_notice_group(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.from_user is None or query.message is None:
        return
    group_id = query.data.split(":", 1)[1]
    async with async_session_maker() as session:
        if await _require_admin_callback(query, session) is None:
            return
    await state.set_state(NoticeFSM.text)
    await state.update_data(group_id=group_id, person_id=None)
    await query.answer()
    await query.message.answer("Текст сообщения:")


@router.message(NoticeFSM.text)
async def fsm_notice_text(message: Message, state: FSMContext) -> None:
    if message.text is None or message.from_user is None or message.bot is None:
        return
    data = await state.get_data()
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        if data.get("group_id"):
            audience = await group_audience(session, data["group_id"])
        elif data.get("person_id"):
            person = await session.get(Person, data["person_id"])
            audience = [person] if person is not None else []
        else:
            audience = []
    report, _, _ = await _send_notice(
        message.bot, audience, free_notice_text(message.text)
    )
    await state.clear()
    await message.answer(report)


@router.message(F.text == BTN_REMINDERS)
async def menu_reminders(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        if await _require_admin_message(message, session) is None:
            return
        today = _today_local()
        summary = await list_today_gather_summary(session, today)
        if not summary:
            await message.answer("На сегодня реакций на напоминания пока нет.")
            return
        pick_rows = [
            PickRow(id=rem.id, full_name=person_row.full_name, detail=detail)
            for rem, person_row, detail in summary
        ]
    await state.clear()
    await state.set_state(ListPickFSM.waiting)
    await state.update_data(
        pick_ids=[row.id for row in pick_rows],
        pick_page=0,
        pick_purpose="reminders",
        pick_day=today.isoformat(),
    )
    body = page_text(pick_rows, 0, HINT_MANY)
    kb = _list_pick_keyboard(pick_rows, 0, "reminders")
    await message.answer(
        "Номер или несколько номеров через запятую — кому написать. "
        "Без номера сообщение не уйдёт.\n\n"
        f"{body}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "ppl_guests")
async def cb_ppl_guests(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        today = _today_local()
        guest_rows = await guest_day_summary(session, today)
        if not guest_rows:
            await query.answer("Гостей на сегодня нет", show_alert=True)
            return
        pick_rows = []
        for rsvp, guest, school_session in guest_rows:
            phone = guest.phone or "—"
            detail = (
                f"{phone} — {format_school_datetime(school_session.starts_at)}, "
                f"{school_session.place}"
            )
            pick_rows.append(
                PickRow(id=rsvp.id, full_name=guest.full_name, detail=detail)
            )
    await query.answer()
    if query.message is None:
        return
    await state.clear()
    await state.set_state(ListPickFSM.waiting)
    await state.update_data(
        pick_ids=[row.id for row in pick_rows],
        pick_page=0,
        pick_purpose="guests",
        pick_day=today.isoformat(),
    )
    body = page_text(pick_rows, 0, HINT_MANY)
    kb = _list_pick_keyboard(pick_rows, 0, "guests")
    await query.message.answer(
        "Номер или несколько номеров через запятую — кому написать. "
        "Без номера сообщение не уйдёт.\n\n"
        f"{body}",
        reply_markup=kb,
    )


@router.message(GuestPickFSM.number, F.text.regexp(r"^\s*\d+\s*$"))
async def fsm_guest_pick(message: Message, state: FSMContext) -> None:
    from .groups import _session_group_name

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


@router.message(ListPickFSM.notice_text, F.text)
async def fsm_list_pick_notice_text(message: Message, state: FSMContext) -> None:
    if message.text is None or message.from_user is None or message.bot is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    data = await state.get_data()
    purpose = str(data.get("notice_purpose") or "")
    pick_ids: list[str] = list(data.get("notice_pick_ids") or [])  # type: ignore[arg-type]
    picked = [PickRow(id=pid, full_name="") for pid in pick_ids]
    async with async_session_maker() as session:
        audience = await _persons_for_notice_pick(session, purpose, picked)
    body = free_notice_text(message.text)
    report, delivered_names, failed_names = await _send_notice(
        message.bot, audience, body
    )
    summary = short_report(
        "Сообщение ушло",
        delivered_names,
        failed=failed_names or None,
    )
    await state.clear()
    await message.answer(f"{report}\n{summary}")


register_pick_spec(
    "msg",
    PickSpec(
        is_index_pick=False,
        build_rows=_build_rows_msg,
        is_notice_multi=True,
        notice_recipient=_notice_recipient_msg,
        on_many_pick=_on_many_pick_notice,
    ),
)
register_pick_spec(
    "guests",
    PickSpec(
        is_index_pick=False,
        build_rows=_build_rows_guests,
        is_notice_multi=True,
        notice_recipient=_notice_recipient_guests,
        on_many_pick=_on_many_pick_notice,
    ),
)
register_pick_spec(
    "reminders",
    PickSpec(
        is_index_pick=False,
        build_rows=_build_rows_reminders,
        is_notice_multi=True,
        notice_recipient=_notice_recipient_reminders,
        on_many_pick=_on_many_pick_notice,
    ),
)
