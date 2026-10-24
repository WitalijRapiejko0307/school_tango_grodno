"""Telegram groups domain: groups, slots, schedule, roster, assignment."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import (
    Attendance,
    Group,
    GroupMembership,
    Person,
    Reminder,
    SchoolSession,
    Stream,
    WeeklySlot,
)
from app.services.attendance import mark_attendance
from app.services.attendance_sheet import build_group_attendance_workbook
from app.services.notices import (
    cancel_notice_text,
    group_audience,
    notice_audience,
    reschedule_notice_text,
    upcoming_sessions,
)
from app.services.notifications import answer_attendance
from app.services.ocr import extract_roster_lines
from app.services.pick_list import (
    HINT_INDEX,
    PickRow,
    attendance_report,
    short_report,
)
from app.services.billing import person_has_unpaid_started
from app.services.roster import (
    RosterDraftItem,
    confirm_roster,
    parse_roster_line,
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
from app.services.school_time import format_school_date, format_school_datetime, school_tz

from .common import (
    BTN_GROUPS,
    BTN_MY_GROUP,
    BTN_SCHEDULE,
    REFUSAL,
    PickSpec,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _list_pick_rows_from_data,
    _parse_hhmm,
    _person_by_telegram,
    _pick_ids_from_data,
    _require_admin_callback,
    _require_admin_message,
    _require_admin_message_strict,
    _school_tz,
    _send_notice,
    _start_list_pick,
    _today_local,
    _weekday_keyboard,
    register_att_pick_spec,
    register_pick_spec,
    reply_markup_for_role,
)
from .states import (
    GroupRenameFSM,
    GroupStreamFSM,
    GuestPickFSM,
    NewGroupFSM,
    PeoplePickFSM,
    RosterFSM,
    TabelFSM,
    WeekFSM,
)

logger = logging.getLogger(__name__)

router = Router(name="telegram.groups")

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())

from .billing import (  # noqa: E402
    _prompt_open_sub_products,
    _report_open_subscription,
)
from .people import (  # noqa: E402
    _active_membership,
    _format_contacts,
    _list_non_admin_pick_rows,
    _prompt_leave_person,
    _start_people_pick,
    send_person_card,
)

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


async def _list_active_groups(session: AsyncSession) -> list[Group]:
    return list(
        (
            await session.execute(
                select(Group)
                .where(Group.archived_at.is_(None))
                .order_by(Group.name)
            )
        ).scalars().all()
    )


async def _list_archived_groups(session: AsyncSession) -> list[Group]:
    return list(
        (
            await session.execute(
                select(Group)
                .where(Group.archived_at.is_not(None))
                .order_by(Group.name)
            )
        ).scalars().all()
    )


_WEEKDAY_FULL_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _format_user_session_datetime(dt: datetime) -> str:
    tz = _school_tz()
    if dt.tzinfo is None:
        local = dt.replace(tzinfo=tz)
    else:
        local = dt.astimezone(tz)
    weekday = _WEEKDAY_FULL_RU[local.weekday()]
    return f"{weekday}, {format_school_datetime(dt)}"


def _format_user_session_date(d: date) -> str:
    return f"{_WEEKDAY_FULL_RU[d.weekday()]}, {format_school_date(d)}"


def _roster_role_label(person: Person, has_active_membership: bool) -> str:
    if person.role == "guest":
        return "гость"
    if person.role == "client":
        return "клиент"
    if person.role == "admin":
        if has_active_membership:
            return "админ, клиент"
        return "админ"
    return person.role


async def _format_roster_member_line(
    session: AsyncSession,
    index: int,
    person: Person,
    today: date,
) -> str:
    membership = await _active_membership(session, person.id)
    role = _roster_role_label(person, membership is not None)
    line = f"{index}. {person.full_name} — {role}"
    if await person_has_unpaid_started(session, person.id, today):
        line += " — не оплачен"
    return line


def _grp_roster_keyboard(group_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить",
                    callback_data=f"grp_roster_add:{group_id}",
                ),
                InlineKeyboardButton(
                    text="Просмотр",
                    callback_data=f"grp_roster_view:{group_id}",
                ),
            ],
        ]
    )


def _weekly_slot_label(slot: WeeklySlot) -> str:
    label = (
        f"{WEEKDAY_LABELS[slot.weekday]} {slot.start_time.strftime('%H:%M')}"
        f"–{slot.end_time.strftime('%H:%M')}, {slot.place}"
    )
    return label


def _session_impact_text(cancelled: int, kept: int) -> str:
    return (
        f"Будет отменено занятий: {cancelled}. "
        f"Останется без изменений: {kept}."
    )


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


def _attendance_pick_rows(
    members: list[tuple[Person, Attendance | None]],
) -> list[PickRow]:
    rows: list[PickRow] = []
    for person, att in members:
        if person.telegram_user_id is None:
            detail = "ещё не в боте"
        elif att is not None:
            detail = "отмечен"
        else:
            detail = "не отмечен"
        rows.append(PickRow(id=person.id, full_name=person.full_name, detail=detail))
    return rows


async def _build_rows_grp(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    rows: list[PickRow] = []
    for group_id in _pick_ids_from_data(data):
        group = await session.get(Group, group_id)
        if group is not None and group.archived_at is None:
            rows.append(PickRow(id=group.id, full_name=group.name))
    return rows


async def _build_rows_active_groups(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    rows: list[PickRow] = []
    for group_id in _pick_ids_from_data(data):
        group = await session.get(Group, group_id)
        if group is not None and group.archived_at is None:
            rows.append(PickRow(id=group.id, full_name=group.name))
    return rows


async def _build_rows_school_sessions(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    rows: list[PickRow] = []
    for school_session_id in _pick_ids_from_data(data):
        school_session = await session.get(SchoolSession, school_session_id)
        if school_session is None:
            continue
        gname = await _session_group_name(session, school_session.group_id)
        detail = (
            f"{_format_user_session_datetime(school_session.starts_at)}, "
            f"{school_session.place}"
        )
        rows.append(PickRow(id=school_session.id, full_name=gname, detail=detail))
    return rows


async def _build_rows_weekly_slots(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    rows: list[PickRow] = []
    for slot_id in _pick_ids_from_data(data):
        slot = await session.get(WeeklySlot, slot_id)
        if slot is None or not slot.active:
            continue
        group = await session.get(Group, slot.group_id)
        if group is None or group.archived_at is not None:
            continue
        detail = (
            f"{WEEKDAY_LABELS[slot.weekday]} "
            f"{slot.start_time.strftime('%H:%M')}, {slot.place}"
        )
        if slot.notes:
            detail = f"{detail} ({slot.notes})"
        rows.append(PickRow(id=slot.id, full_name=group.name, detail=detail))
    return rows


async def _build_rows_att(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    purpose = str(data.get("pick_purpose") or "")
    school_session_id = purpose.split(":", 1)[1]
    sess = await session.get(SchoolSession, school_session_id)
    if sess is None:
        return []
    members = await _members_with_attendance(session, sess.group_id, school_session_id)
    return _attendance_pick_rows(members)


async def _on_index_pick_grp(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _send_group_card(message, picked[0].id)


async def _on_index_pick_today(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    from .core import _start_admin_attendance_pick

    await _start_admin_attendance_pick(message, state, picked[0].id)


async def _on_index_pick_sess(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    from .notices import _send_notice_session_actions

    await _send_notice_session_actions(message, picked[0].id)


async def _on_index_pick_slot_week(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await state.update_data(mode="edit_all", slot_id=picked[0].id)
    await message.answer(
        "Новый день недели:",
        reply_markup=_weekday_keyboard("wday"),
    )


async def _on_index_pick_slot_once(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await state.update_data(mode="edit_one", slot_id=picked[0].id)
    await state.set_state(WeekFSM.once_date)
    await message.answer("Дата занятия (ДД-ММ-ГГГГ):")


async def _on_index_pick_grp_slot(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await state.update_data(mode="new", group_id=picked[0].id)
    await message.answer(
        "День недели:",
        reply_markup=_weekday_keyboard("wday"),
    )


async def _on_index_pick_grp_stream(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    group_id = picked[0].id
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None:
            await message.answer("Не найдено")
            return
        kb = await _stream_pick_keyboard(
            session, mode="edit_group", group_id=group_id
        )
    await state.update_data(edit_group_id=group_id)
    await message.answer(f"Поток для группы «{group.name}»:", reply_markup=kb)


async def _on_index_pick_grp_assign(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _apply_assign_group_choice(message, state, picked[0].id)




async def _on_index_pick_sub_partner(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _after_people_pick(message, state, picked[0].id, "sub_partner")


async def _on_many_pick_att(
    message: Message,
    state: FSMContext,
    picked: list[PickRow],
    admin: Person,
) -> None:
    data = await state.get_data()
    purpose = str(data.get("pick_purpose") or "")
    school_session_id = purpose.split(":", 1)[1]
    async with async_session_maker() as session:
        rows = await _list_pick_rows_from_data(session, data)
    number_by_id = {row.id: index + 1 for index, row in enumerate(rows)}
    report_lines: list[tuple[int, str, str]] = []
    async with async_session_maker() as session:
        for row in picked:
            number = number_by_id[row.id]
            person = await session.get(Person, row.id)
            if person is None:
                continue
            if person.telegram_user_id is None:
                report_lines.append((number, row.full_name, "не в боте"))
                continue
            att_result = await session.execute(
                select(Attendance).where(
                    Attendance.person_id == person.id,
                    Attendance.session_id == school_session_id,
                )
            )
            existing = att_result.scalar_one_or_none()
            if existing is not None:
                await session.delete(existing)
                outcome = "снят"
            else:
                await mark_attendance(
                    session,
                    person.id,
                    school_session_id,
                    "admin",
                    marked_by_id=admin.id,
                )
                outcome = "отмечен"
            report_lines.append((number, row.full_name, outcome))
        await session.commit()
    await message.answer(attendance_report(report_lines))


register_pick_spec(
    "grp",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_grp,
        on_index_pick=_on_index_pick_grp,
    ),
)
register_pick_spec(
    "today",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_school_sessions,
        on_index_pick=_on_index_pick_today,
    ),
)
register_pick_spec(
    "sess",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_school_sessions,
        on_index_pick=_on_index_pick_sess,
    ),
)
register_pick_spec(
    "slot_week",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_weekly_slots,
        on_index_pick=_on_index_pick_slot_week,
    ),
)
register_pick_spec(
    "slot_once",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_weekly_slots,
        on_index_pick=_on_index_pick_slot_once,
    ),
)
register_pick_spec(
    "grp_slot",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_active_groups,
        on_index_pick=_on_index_pick_grp_slot,
    ),
)
register_pick_spec(
    "grp_stream",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_active_groups,
        on_index_pick=_on_index_pick_grp_stream,
    ),
)
register_pick_spec(
    "grp_assign",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_active_groups,
        on_index_pick=_on_index_pick_grp_assign,
    ),
)
from .notices import _build_rows_msg  # noqa: E402

register_pick_spec(
    "sub_partner",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_msg,
        on_index_pick=_on_index_pick_sub_partner,
    ),
)

_ATT_PICK_SPEC = PickSpec(
    is_index_pick=False,
    more_callback="att_more",
    build_rows=_build_rows_att,
    on_many_pick=_on_many_pick_att,
)
register_att_pick_spec(_ATT_PICK_SPEC)

async def _prompt_assign_group(
    message: Message, state: FSMContext, person_id: str
) -> None:
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await message.answer("Нет групп")
        return
    await state.update_data(person_id=person_id)
    rows = [PickRow(id=g.id, full_name=g.name) for g in groups]
    await _start_list_pick(
        message,
        state,
        purpose="grp_assign",
        rows=rows,
        title="Номер выберет группу.",
        hint=HINT_INDEX,
    )




async def _after_people_pick(
    message: Message, state: FSMContext, person_id: str, purpose: str
) -> None:
    if purpose == "assign":
        await _prompt_assign_group(message, state, person_id)
        return
    if purpose == "subscription":
        await _prompt_open_sub_products(message, state, person_id)
        return
    if purpose == "sub_partner":
        data = await state.get_data()
        primary_id = data.get("person_id")
        product_id = data.get("product_id")
        if not primary_id or not product_id:
            await state.clear()
            await message.answer("Ошибка")
            return
        await _report_open_subscription(
            message,
            state,
            person_id=str(primary_id),
            product_id=str(product_id),
            partner_id=person_id,
        )
        return
    if purpose == "card":
        await state.set_state(PeoplePickFSM.card_name)
        await state.update_data(card_person_id=person_id)
        await message.answer('Имя и фамилия. «то же» — не менять.')
        return
    if purpose == "view":
        await state.clear()
        await send_person_card(message, person_id)
        return
    if purpose == "leave":
        await _prompt_leave_person(message, state, person_id)


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


async def _finish_assign_group_message(
    message: Message,
    state: FSMContext,
    person_id: str,
    group_id: str,
) -> None:
    today = _today_local()
    guest_notify_id: int | None = None
    transfer_notify: tuple[int, str, str, str | None] | None = None
    async with async_session_maker() as session:
        target = await session.get(Person, person_id)
        was_guest = target is not None and target.role == "guest"
        prev_membership = await _active_membership(session, person_id)
        old_group_name: str | None = None
        if prev_membership is not None:
            old_group_name = await _session_group_name(
                session, prev_membership.group_id
            )
        new_group_name = await _session_group_name(session, group_id)
        await assign_group(session, person_id, group_id, today)
        if (
            target is not None
            and target.telegram_user_id is not None
            and prev_membership is not None
            and old_group_name is not None
        ):
            await materialize_range(session, today, today + timedelta(days=21))
            now = datetime.now(UTC)
            next_result = await session.execute(
                select(SchoolSession)
                .where(
                    SchoolSession.group_id == group_id,
                    SchoolSession.starts_at > now,
                    SchoolSession.status == "scheduled",
                )
                .order_by(SchoolSession.starts_at)
                .limit(1)
            )
            nxt = next_result.scalar_one_or_none()
            next_line: str | None = None
            if nxt is not None:
                next_line = (
                    f"Ближайшее занятие: {_format_user_session_datetime(nxt.starts_at)}, "
                    f"{nxt.place}"
                )
            transfer_notify = (
                target.telegram_user_id,
                old_group_name,
                new_group_name,
                next_line,
            )
        elif (
            was_guest
            and prev_membership is None
            and target is not None
            and target.telegram_user_id is not None
        ):
            guest_notify_id = target.telegram_user_id
        await session.commit()
    await state.clear()
    await message.answer("Человек назначен в группу.")
    if guest_notify_id is not None and message.bot is not None:
        try:
            await message.bot.send_message(
                guest_notify_id,
                "Вас записали в группу. Теперь вы клиент школы.",
                reply_markup=reply_markup_for_role("client"),
            )
        except Exception:
            logger.exception("Failed to refresh client menu for %s", guest_notify_id)
    if transfer_notify is not None and message.bot is not None:
        tid, from_name, to_name, next_line = transfer_notify
        lines = [
            f"Вас перевели из группы «{from_name}» в группу «{to_name}».",
        ]
        if next_line is not None:
            lines.append(next_line)
        try:
            await message.bot.send_message(tid, "\n".join(lines))
        except Exception:
            logger.exception("Failed to notify group transfer for %s", tid)


async def _apply_assign_group_choice(
    message: Message, state: FSMContext, group_id: str
) -> None:
    data = await state.get_data()
    person_id = data.get("person_id")
    if not person_id:
        await message.answer("Сначала выберите человека")
        return
    async with async_session_maker() as session:
        target = await session.get(Person, person_id)
        new_group = await session.get(Group, group_id)
        if target is None or new_group is None:
            await message.answer("Не найдено")
            return
        membership = await _active_membership(session, person_id)
        if membership is not None and membership.group_id == group_id:
            await message.answer("Уже в этой группе")
            return
        if membership is not None:
            old_group = await session.get(Group, membership.group_id)
            if old_group is None:
                await message.answer("Ошибка")
                return
            text = group_transfer_confirmation_text(
                target.full_name,
                old_group.name,
                membership.started_on,
                new_group.name,
            )
            await state.update_data(assign_group_id=group_id)
            await message.answer(text, reply_markup=_transfer_confirm_keyboard())
            return
    await _finish_assign_group_message(message, state, person_id, group_id)




async def _begin_roster_group_state(state: FSMContext, group_id: str) -> None:
    await state.set_state(RosterFSM.active)
    await state.update_data(
        group_id=group_id,
        drafts=[],
        roster_mode=None,
        roster_view_ids=None,
        roster_view_group=None,
    )


async def _begin_roster_group(
    message: Message, state: FSMContext, group_id: str
) -> None:
    await _begin_roster_group_state(state, group_id)
    await message.answer(
        "Пришлите строки состава (по одному человеку на строку) или фото списка."
    )


async def _finish_assign_group(
    query: CallbackQuery,
    state: FSMContext,
    person_id: str,
    group_id: str,
) -> None:
    today = _today_local()
    guest_notify_id: int | None = None
    transfer_notify: tuple[int, str, str, str | None] | None = None
    async with async_session_maker() as session:
        target = await session.get(Person, person_id)
        was_guest = target is not None and target.role == "guest"
        prev_membership = await _active_membership(session, person_id)
        old_group_name: str | None = None
        if prev_membership is not None:
            old_group_name = await _session_group_name(
                session, prev_membership.group_id
            )
        new_group_name = await _session_group_name(session, group_id)
        await assign_group(session, person_id, group_id, today)
        if (
            target is not None
            and target.telegram_user_id is not None
            and prev_membership is not None
            and old_group_name is not None
        ):
            await materialize_range(session, today, today + timedelta(days=21))
            now = datetime.now(UTC)
            next_result = await session.execute(
                select(SchoolSession)
                .where(
                    SchoolSession.group_id == group_id,
                    SchoolSession.starts_at > now,
                    SchoolSession.status == "scheduled",
                )
                .order_by(SchoolSession.starts_at)
                .limit(1)
            )
            nxt = next_result.scalar_one_or_none()
            next_line: str | None = None
            if nxt is not None:
                next_line = (
                    f"Ближайшее занятие: {_format_user_session_datetime(nxt.starts_at)}, "
                    f"{nxt.place}"
                )
            transfer_notify = (
                target.telegram_user_id,
                old_group_name,
                new_group_name,
                next_line,
            )
        elif (
            was_guest
            and prev_membership is None
            and target is not None
            and target.telegram_user_id is not None
        ):
            guest_notify_id = target.telegram_user_id
        await session.commit()
    await state.clear()
    await query.answer("Готово")
    if query.message:
        await query.message.answer("Человек назначен в группу.")
    if guest_notify_id is not None:
        try:
            await query.bot.send_message(
                guest_notify_id,
                "Вас записали в группу. Теперь вы клиент школы.",
                reply_markup=reply_markup_for_role("client"),
            )
        except Exception:
            logger.exception("Failed to refresh client menu for %s", guest_notify_id)
    if transfer_notify is not None:
        tid, from_name, to_name, next_line = transfer_notify
        lines = [
            f"Вас перевели из группы «{from_name}» в группу «{to_name}».",
        ]
        if next_line is not None:
            lines.append(next_line)
        try:
            await query.bot.send_message(tid, "\n".join(lines))
        except Exception:
            logger.exception("Failed to notify group transfer for %s", tid)




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
    await mark_attendance(
        session, person_id, school_session_id, "client", marked_by_id=person_id
    )
    return "Отметили по вашему ответу."


_ROSTER_DELETE_RE = re.compile(r"^\s*удалить\s+(\d+)\s*$", re.IGNORECASE)
_ROSTER_REPLACE_RE = re.compile(r"^\s*(\d+)\s*[.:)]\s*(.+)$", re.IGNORECASE)


def _roster_draft_to_dict(item: RosterDraftItem) -> dict[str, object]:
    return {
        "full_name": item.full_name,
        "name_key": item.name_key,
        "needs_fix": item.needs_fix,
        "note": item.note,
    }


def _roster_drafts_from_dicts(raw: list[dict[str, object]]) -> list[RosterDraftItem]:
    out: list[RosterDraftItem] = []
    for d in raw:
        nk = d.get("name_key")
        out.append(
            RosterDraftItem(
                full_name=str(d["full_name"]),
                name_key=None if nk is None else str(nk),
                needs_fix=bool(d.get("needs_fix")),
                note=str(d.get("note") or ""),
            )
        )
    return out


def _parse_lines_to_draft_dicts(text: str) -> list[dict[str, object]]:
    drafts: list[dict[str, object]] = []
    for line in text.splitlines():
        for item in parse_roster_line(line):
            drafts.append(_roster_draft_to_dict(item))
    return drafts


def _format_roster_preview(drafts: list[dict[str, object]]) -> str:
    if not drafts:
        return "Черновик пуст."
    lines = ["Черновик состава:"]
    for index, d in enumerate(drafts, start=1):
        mark = " — нужна правка" if d.get("needs_fix") else ""
        note = str(d.get("note") or "").strip()
        note_part = f" ({note})" if note else ""
        lines.append(f"{index}. {d['full_name']}{mark}{note_part}")
    lines.append("")
    lines.append(
        "«готово» — сохранить, «отмена» — отменить, «удалить N», "
        "«N. фамилия имя» — заменить строку, или новые строки для добавления."
    )
    return "\n".join(lines)


def _apply_roster_replacements(
    drafts: list[dict[str, object]], text: str
) -> tuple[list[dict[str, object]], str | None]:
    """Apply numbered lines like ``30. Рапейко Виталий`` onto an existing draft.

    Returns the updated draft and an error message when a number is missing.
    ``None`` as the draft means the message is not a set of replacements.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    edits: list[tuple[int, str]] = []
    for line in lines:
        match = _ROSTER_REPLACE_RE.match(line)
        if match is None:
            return drafts, None
        edits.append((int(match.group(1)), match.group(2).strip()))
    if not edits:
        return drafts, None
    updated = list(drafts)
    for index, body in sorted(edits, key=lambda item: item[0], reverse=True):
        if not 1 <= index <= len(updated):
            return drafts, f"Нет строки с таким номером: {index}."
        new_items = _parse_lines_to_draft_dicts(body)
        if not new_items:
            return drafts, "Пустая строка."
        updated[index - 1 : index] = new_items
    return updated, ""



@router.callback_query(F.data.startswith("pick_roster_grp:"))
async def cb_pick_roster_group(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(RosterFSM.active)
    await state.update_data(group_id=group_id, drafts=[])
    await query.answer()
    if query.message:
        await query.message.answer(
            "Пришлите строки состава (по одному человеку на строку) или фото списка."
        )


@router.callback_query(F.data.startswith("pick_grp:"))
async def cb_pick_group_slot(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await state.update_data(mode="new", group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            "День недели:",
            reply_markup=_weekday_keyboard("wday"),
        )


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
    if await _require_admin_callback(query) is None:
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
            await state.update_data(assign_group_id=group_id)
            await query.answer()
            if query.message:
                await query.message.answer(
                    text, reply_markup=_transfer_confirm_keyboard()
                )
            return
    await _finish_assign_group(query, state, person_id, group_id)


def _transfer_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перевести", callback_data="assign_go")],
            [InlineKeyboardButton(text="Отмена", callback_data="assign_cancel")],
        ]
    )


@router.callback_query(F.data == "assign_go")
async def cb_assign_group_confirm(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    person_id = data.get("person_id")
    group_id = data.get("assign_group_id")
    if not person_id or not group_id:
        await query.answer("Сначала выберите человека", show_alert=True)
        return
    if await _require_admin_callback(query) is None:
        return
    await _finish_assign_group(query, state, person_id, group_id)


@router.callback_query(F.data == "assign_cancel")
async def cb_assign_group_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Перевод в другую группу отменён.")




@router.callback_query(F.data == "admin_new_group")
async def cb_admin_new_group(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(NewGroupFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название новой группы:")


@router.callback_query(F.data == "admin_slot")
async def cb_admin_slot(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await query.answer("Сначала создайте группу", show_alert=True)
        return
    rows = [PickRow(id=g.id, full_name=g.name) for g in groups]
    await query.answer()
    if query.message:
        await _start_list_pick(
            query.message,
            state,
            purpose="grp_slot",
            rows=rows,
            title="Номер выберет группу.",
            hint=HINT_INDEX,
        )


@router.callback_query(F.data == "people_assign")
async def cb_people_assign(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        rows = await _list_non_admin_pick_rows(session)
    if not rows:
        await query.answer("Нет людей", show_alert=True)
        return
    await query.answer()
    if query.message:
        await _start_people_pick(
            query.message,
            state,
            purpose="assign",
            rows=rows,
            title="Запись в группу по номеру.",
        )

def _group_card_keyboard(group_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить", callback_data=f"grp_ren:{group_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Состав", callback_data=f"grp_roster:{group_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Задать поток", callback_data=f"grp_stream:{group_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Добавить слот", callback_data=f"grp_slotadd:{group_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Изменить слот на все недели",
                    callback_data=f"grp_week:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Выключить слот", callback_data=f"slot_grp:{group_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Перенести или отменить одно занятие",
                    callback_data=f"grp_once:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Расписание",
                    callback_data=f"grp_sched:{group_id}",
                ),
                InlineKeyboardButton(
                    text="Табель",
                    callback_data=f"grp_tabel:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Убрать группу", callback_data=f"grp_ask:{group_id}"
                ),
            ],
        ]
    )


async def _send_group_card(message: Message, group_id: str) -> None:
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await message.answer("Не найдено")
            return
        name = group.name
    await message.answer(name, reply_markup=_group_card_keyboard(group_id))


@router.message(F.text == BTN_GROUPS)
async def menu_groups(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        if await _require_admin_message(message, session) is None:
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Список групп", callback_data="grp_list")],
            [InlineKeyboardButton(text="Новая группа", callback_data="admin_new_group")],
            [InlineKeyboardButton(text="Архив групп", callback_data="grp_arch")],
        ]
    )
    await message.answer("Группы:", reply_markup=kb)


@router.callback_query(F.data == "grp_list")
async def cb_grp_list(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await query.answer("Нет групп", show_alert=True)
        return
    rows = [PickRow(id=g.id, full_name=g.name) for g in groups]
    await query.answer()
    if query.message:
        await _start_list_pick(
            query.message,
            state,
            purpose="grp",
            rows=rows,
            title="Номер откроет карточку группы.",
            hint=HINT_INDEX,
        )


@router.callback_query(F.data.startswith("grp_card:"))
async def cb_grp_card(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer()
    if query.message:
        await _send_group_card(query.message, group_id)


@router.callback_query(F.data.startswith("grp_roster:"))
async def cb_grp_roster(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        name = group.name
        members = await group_audience(session, group_id)
        today = _today_local()
        member_lines: list[str] = []
        for index, person in enumerate(members, start=1):
            member_lines.append(
                await _format_roster_member_line(session, index, person, today)
            )
    await state.clear()
    await query.answer()
    if query.message is None:
        return
    if member_lines:
        lines = [f"Состав группы «{name}»:"] + member_lines
    else:
        lines = [f"Состав группы «{name}»: пока никого нет."]
    await query.message.answer("\n".join(lines), reply_markup=_grp_roster_keyboard(group_id))


@router.callback_query(F.data.startswith("grp_roster_add:"))
async def cb_grp_roster_add(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer()
    if query.message:
        await _begin_roster_group(query.message, state, group_id)


@router.callback_query(F.data.startswith("grp_roster_view:"))
async def cb_grp_roster_view(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        members = await group_audience(session, group_id)
    view_ids = [p.id for p in members]
    await state.set_state(RosterFSM.active)
    await state.update_data(
        roster_mode="view",
        roster_view_ids=view_ids,
        roster_view_group=group_id,
        group_id=group_id,
        drafts=[],
    )
    await query.answer()
    if query.message:
        await query.message.answer(
            "Номер откроет карточку.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отмена", callback_data="grp_roster_view_cancel"
                        )
                    ]
                ]
            ),
        )


@router.callback_query(F.data == "grp_roster_view_cancel")
async def cb_grp_roster_view_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer("Выбор отменён.")


@router.callback_query(F.data.startswith("grp_stream:"))
async def cb_grp_stream(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        kb = await _stream_pick_keyboard(
            session, mode="edit_group", group_id=group_id
        )
        gname = group.name
    await state.update_data(edit_group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(f"Поток для группы «{gname}»:", reply_markup=kb)


@router.callback_query(F.data.startswith("grp_slotadd:"))
async def cb_grp_slotadd(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await state.update_data(mode="new", group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            "День недели:",
            reply_markup=_weekday_keyboard("wday"),
        )


@router.callback_query(F.data.startswith("grp_week:"))
async def cb_grp_week(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        pick_rows = await _weekly_slot_pick_rows(session, group_id=group_id)
    if not pick_rows:
        await query.answer("Нет недельных слотов", show_alert=True)
        return
    await state.update_data(mode="edit_all")
    await query.answer()
    if query.message:
        await query.message.answer("Какой слот изменить на все недели?")
        await _start_list_pick(
            query.message,
            state,
            purpose="slot_week",
            rows=pick_rows,
            title="Номер откроет этот слот.",
            hint=HINT_INDEX,
        )


@router.callback_query(F.data.startswith("grp_once:"))
async def cb_grp_once(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        pick_rows = await _weekly_slot_pick_rows(session, group_id=group_id)
    if not pick_rows:
        await query.answer("Нет недельных слотов", show_alert=True)
        return
    await query.answer()
    if query.message:
        await query.message.answer(
            "Какое повторяющееся занятие изменить на одну дату?"
        )
        await _start_list_pick(
            query.message,
            state,
            purpose="slot_once",
            rows=pick_rows,
            title="Номер откроет этот слот.",
            hint=HINT_INDEX,
        )


@router.callback_query(F.data.startswith("grp_sched:"))
async def cb_grp_schedule(query: CallbackQuery) -> None:
    """Read-only view of the group's upcoming sessions (no actions)."""
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        today = _today_local()
        now = datetime.now(UTC)
        await materialize_range(session, today, today + timedelta(days=21))
        upcoming = await upcoming_sessions(session, now, group_id=group_id)
        await session.commit()
        gname = group.name
    await query.answer()
    if query.message is None:
        return
    if not upcoming:
        await query.message.answer(
            f"«{gname}»: предстоящих занятий на ближайшие 21 день нет."
        )
        return
    lines = [f"Расписание «{gname}» на ближайшие 21 день:"]
    for school_session, _group_name in upcoming:
        lines.append(
            f"• {_format_user_session_datetime(school_session.starts_at)}, "
            f"{school_session.place}"
        )
    await query.message.answer("\n".join(lines))


def _tabel_period_keyboard(group_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Этот месяц",
                    callback_data=f"grp_tabel_m:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Прошлый месяц",
                    callback_data=f"grp_tabel_p:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Свои даты",
                    callback_data=f"grp_tabel_c:{group_id}",
                ),
            ],
        ]
    )


def _tabel_this_month_range() -> tuple[date, date]:
    today = datetime.now(school_tz()).date()
    return today.replace(day=1), today


def _tabel_prev_month_range() -> tuple[date, date]:
    today = datetime.now(school_tz()).date()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


async def _send_group_tabel(
    message: Message,
    state: FSMContext,
    group_id: str,
    date_from: date,
    date_to: date,
) -> None:
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await state.clear()
            await message.answer("Не найдено")
            return
        gname = group.name
        workbook = await build_group_attendance_workbook(
            session, group_id, date_from, date_to
        )
        await session.commit()
    await state.clear()
    if workbook is None:
        await message.answer("В этом периоде нет занятий.")
        return
    caption = (
        f"{gname}\n{format_school_date(date_from)} — {format_school_date(date_to)}"
    )
    await message.answer_document(
        BufferedInputFile(workbook, filename="Tabel.xlsx"),
        caption=caption,
    )


@router.callback_query(F.data.startswith("grp_tabel:"))
async def cb_grp_tabel(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer()
    if query.message:
        await query.message.answer(
            "Период табеля:",
            reply_markup=_tabel_period_keyboard(group_id),
        )


@router.callback_query(F.data.startswith("grp_tabel_m:"))
async def cb_grp_tabel_this_month(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.message is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    date_from, date_to = _tabel_this_month_range()
    await query.answer()
    await _send_group_tabel(query.message, state, group_id, date_from, date_to)


@router.callback_query(F.data.startswith("grp_tabel_p:"))
async def cb_grp_tabel_prev_month(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.message is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    date_from, date_to = _tabel_prev_month_range()
    await query.answer()
    await _send_group_tabel(query.message, state, group_id, date_from, date_to)


@router.callback_query(F.data.startswith("grp_tabel_c:"))
async def cb_grp_tabel_custom(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.message is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await state.set_state(TabelFSM.date_from)
    await state.update_data(tabel_group_id=group_id)
    await query.answer()
    await query.message.answer("Дата с (ДД.ММ.ГГГГ):")


@router.message(TabelFSM.date_from)
async def fsm_tabel_date_from(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if message.text.strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Создание табеля отменено.")
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    try:
        date_from = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ")
        return
    await state.update_data(tabel_date_from=date_from.isoformat())
    await state.set_state(TabelFSM.date_to)
    await message.answer("Дата по (ДД.ММ.ГГГГ):")


@router.message(TabelFSM.date_to)
async def fsm_tabel_date_to(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if message.text.strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Создание табеля отменено.")
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    try:
        date_to = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ")
        return
    data = await state.get_data()
    group_id = data.get("tabel_group_id")
    raw_from = data.get("tabel_date_from")
    if not group_id or not raw_from:
        await state.clear()
        await message.answer("Сначала выберите группу.")
        return
    date_from = date.fromisoformat(str(raw_from))
    if date_to < date_from:
        await message.answer("Дата «по» раньше даты «с».")
        return
    await _send_group_tabel(message, state, str(group_id), date_from, date_to)


@router.callback_query(F.data.startswith("grp_ren:"))
async def cb_grp_rename(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await state.set_state(GroupRenameFSM.waiting_name)
    await state.update_data(group_id=group_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            "Новое название. Чтобы оставить прежнее, отправьте «то же»."
        )


@router.message(GroupRenameFSM.waiting_name)
async def fsm_grp_rename(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    group_id = data.get("group_id")
    if not group_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await message.answer("Группа не найдена.")
            await state.clear()
            return
        if message.text.strip().lower() != "то же":
            group = await rename_group(session, group_id, message.text)
            await session.commit()
        name = group.name
    await state.clear()
    await message.answer(short_report(f"Группа теперь «{name}»", []))


@router.callback_query(F.data.startswith("grp_ask:"))
async def cb_grp_archive_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        summary = await count_archive_impact(session, group_id)
        name = group.name
    text = (
        f"Убрать группу «{name}»?\n{_session_impact_text(summary.cancelled, summary.kept)}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать", callback_data=f"grp_yes:{group_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="grp_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "grp_cancel")
async def cb_grp_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Без изменений.")


@router.callback_query(F.data.startswith("grp_yes:"))
async def cb_grp_archive_yes(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    now = datetime.now(UTC)
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        group, summary = await archive_group(session, group_id, now)
        await session.commit()
        name = group.name
        cancelled, kept = summary.cancelled, summary.kept
    await query.answer("Готово")
    if query.message:
        await query.message.answer(
            short_report(
                f"Группа «{name}» в архиве: отменено {cancelled}, "
                f"без изменений {kept}",
                [],
            )
        )


@router.callback_query(F.data == "grp_arch")
async def cb_grp_arch_list(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        groups = await _list_archived_groups(session)
    if not groups:
        await query.answer("Архив пуст", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Вернуть: {g.name}",
                    callback_data=f"grp_back:{g.id}",
                )
            ]
            for g in groups
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Архив групп:", reply_markup=kb)


@router.callback_query(F.data.startswith("grp_back:"))
async def cb_grp_restore(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is None:
            await query.answer("Не найдено", show_alert=True)
            return
        await restore_group(session, group_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(
            f"{short_report('Группа возвращена', [])} "
            "Недельные слоты остаются выключенными."
        )


@router.callback_query(F.data == "slot_off")
async def cb_slot_off(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await query.answer("Нет групп", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=g.name, callback_data=f"slot_grp:{g.id}"
                )
            ]
            for g in groups
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Выберите группу:", reply_markup=kb)


@router.callback_query(F.data.startswith("slot_grp:"))
async def cb_slot_off_group(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        slots = list(
            (
                await session.execute(
                    select(WeeklySlot)
                    .where(
                        WeeklySlot.group_id == group_id,
                        WeeklySlot.active.is_(True),
                    )
                    .order_by(WeeklySlot.weekday, WeeklySlot.start_time)
                )
            ).scalars().all()
        )
        gname = group.name
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_weekly_slot_label(s)[:60],
                callback_data=f"slot_ask:{s.id}",
            )
        ]
        for s in slots
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Архив слотов", callback_data=f"slot_arch:{group_id}"
            )
        ]
    )
    await query.answer()
    if query.message:
        text = (
            f"Активные слоты группы «{gname}»:"
            if slots
            else f"У группы «{gname}» нет активных слотов."
        )
        await query.message.answer(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.callback_query(F.data.startswith("slot_arch:"))
async def cb_slot_arch_list(query: CallbackQuery) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None:
            await query.answer("Не найдено", show_alert=True)
            return
        slots = list(
            (
                await session.execute(
                    select(WeeklySlot)
                    .where(
                        WeeklySlot.group_id == group_id,
                        WeeklySlot.active.is_(False),
                    )
                    .order_by(WeeklySlot.weekday, WeeklySlot.start_time)
                )
            ).scalars().all()
        )
        gname = group.name
    if not slots:
        await query.answer("Архив слотов пуст", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Вернуть: {_weekly_slot_label(s)[:55]}",
                    callback_data=f"slot_back:{s.id}",
                )
            ]
            for s in slots
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Выключенные слоты — «{gname}»:", reply_markup=kb
        )


@router.callback_query(F.data.startswith("slot_ask:"))
async def cb_slot_deactivate_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    slot_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        slot = await session.get(WeeklySlot, slot_id)
        if slot is None or not slot.active:
            await query.answer("Не найдено", show_alert=True)
            return
        summary = await count_slot_deactivate_impact(session, slot_id)
        label = _weekly_slot_label(slot)
    text = (
        f"Выключить слот {label}?\n"
        f"{_session_impact_text(summary.cancelled, summary.kept)}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать", callback_data=f"slot_yes:{slot_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="grp_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("slot_yes:"))
async def cb_slot_deactivate_yes(query: CallbackQuery) -> None:
    if query.data is None:
        return
    slot_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        slot = await session.get(WeeklySlot, slot_id)
        if slot is None or not slot.active:
            await query.answer("Не найдено", show_alert=True)
            return
        await deactivate_weekly_slot(session, slot_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Слот выключен", []))


@router.callback_query(F.data.startswith("slot_back:"))
async def cb_slot_restore(query: CallbackQuery) -> None:
    if query.data is None:
        return
    slot_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        slot = await session.get(WeeklySlot, slot_id)
        if slot is None or slot.active:
            await query.answer("Не найдено", show_alert=True)
            return
        await restore_weekly_slot(session, slot_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Слот возвращён", []))





async def _apply_roster_image(
    message: Message, state: FSMContext, image_bytes: bytes, mime: str
) -> None:
    data = await state.get_data()
    if data.get("roster_mode") == "view":
        await message.answer("Сейчас ожидается номер из списка состава.")
        return
    if not data.get("group_id"):
        await message.answer("Сначала выберите или создайте группу.")
        return
    try:
        lines = await extract_roster_lines(image_bytes, mime)
    except Exception:
        logger.exception("Roster image OCR failed")
        await message.answer(
            "Не удалось прочитать изображение. Пришлите более чёткий снимок "
            "или введите состав текстом — по строке на человека."
        )
        return
    drafts = []
    for line in lines:
        for item in parse_roster_line(line):
            drafts.append(_roster_draft_to_dict(item))
    await state.update_data(drafts=drafts)
    await message.answer(_format_roster_preview(drafts))


@router.message(RosterFSM.active, F.photo)
async def fsm_roster_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo or message.bot is None:
        return
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    downloaded = await message.bot.download_file(file.file_path)
    await _apply_roster_image(message, state, downloaded.read(), "image/jpeg")


@router.message(RosterFSM.active, F.document)
async def fsm_roster_document(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.document is None or message.bot is None:
        return
    mime = message.document.mime_type or ""
    if not mime.startswith("image/"):
        await message.answer(
            "Пришлите фото списка или файл изображения. Другие файлы бот не читает."
        )
        return
    file = await message.bot.get_file(message.document.file_id)
    downloaded = await message.bot.download_file(file.file_path)
    await _apply_roster_image(message, state, downloaded.read(), mime)


@router.message(RosterFSM.active, F.text)
async def fsm_roster_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    text = message.text.strip()
    if not text:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return

    data = await state.get_data()
    if data.get("roster_mode") == "view":
        if text.casefold() == "отмена":
            await state.clear()
            await message.answer("Выбор отменён.")
            return
        view_ids = list(data.get("roster_view_ids") or [])
        if not text.isdigit():
            await message.answer("Введите номер из списка.")
            return
        index = int(text)
        if not 1 <= index <= len(view_ids):
            await message.answer("Нет такого номера.")
            return
        person_id = view_ids[index - 1]
        await state.clear()
        await send_person_card(message, person_id)
        return

    group_id = data.get("group_id")
    drafts: list[dict[str, object]] = list(data.get("drafts") or [])

    if group_id is None:
        async with async_session_maker() as session:
            group = await create_group(session, text)
            await session.commit()
            group_id = group.id
        await state.update_data(group_id=group_id, drafts=[])
        await message.answer(
            f"Группа «{text}» создана.\n"
            "Пришлите строки состава (по одному человеку на строку) или фото списка."
        )
        return

    lowered = text.lower()
    if lowered == "отмена":
        await state.clear()
        await message.answer("Загрузка состава отменена.")
        return

    if lowered == "готово":
        items = _roster_drafts_from_dicts(drafts)
        needs_fix_count = sum(1 for item in items if item.needs_fix or not item.name_key)
        async with async_session_maker() as session:
            result = await confirm_roster(session, group_id, items)
            await session.commit()
        await state.clear()
        parts = [
            f"Добавлено: {result.created}.",
            f"Пропущено: {result.skipped}.",
        ]
        if needs_fix_count:
            parts.append(
                f"Из них {needs_fix_count} с пометкой «нужна правка» не записывались."
            )
        await message.answer(" ".join(parts))
        return

    delete_match = _ROSTER_DELETE_RE.match(text)
    if delete_match:
        index = int(delete_match.group(1))
        if 1 <= index <= len(drafts):
            drafts.pop(index - 1)
            await state.update_data(drafts=drafts)
            await message.answer(_format_roster_preview(drafts))
        else:
            await message.answer("Нет строки с таким номером.")
        return

    replaced, replace_error = _apply_roster_replacements(drafts, text)
    if replace_error is not None:
        if replace_error:
            await message.answer(replace_error)
            return
        drafts = replaced
        await state.update_data(drafts=drafts)
        await message.answer(_format_roster_preview(drafts))
        return

    drafts.extend(_parse_lines_to_draft_dicts(text))
    await state.update_data(drafts=drafts)
    await message.answer(_format_roster_preview(drafts))




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
                f"Группа: {gname}\nБлижайшее: {_format_user_session_datetime(nxt.starts_at)}, {nxt.place}"
            )
        else:
            await message.answer(f"Группа: {gname}\nБлижайших занятий нет.")
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
            local_t = _format_user_session_datetime(school_session.starts_at)
            lines.append(f"{index}. {local_t} — {gname}, {school_session.place}")
            choices[str(index)] = school_session.id
        await state.set_state(GuestPickFSM.number)
        await state.update_data(guest_choices=choices)
        await message.answer("\n".join(lines))
@router.message(NewGroupFSM.name)
async def fsm_new_group(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
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
    if await _require_admin_callback(query) is None:
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
async def cb_admin_group_stream(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await query.answer("Сначала создайте группу", show_alert=True)
        return
    rows = [PickRow(id=g.id, full_name=g.name) for g in groups]
    await query.answer()
    if query.message:
        await _start_list_pick(
            query.message,
            state,
            purpose="grp_stream",
            rows=rows,
            title="Номер выберет группу.",
            hint=HINT_INDEX,
        )


@router.callback_query(F.data.startswith("pick_grp_strm:"))
async def cb_pick_group_stream(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    group_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
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
    if await _require_admin_callback(query) is None:
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
        if await _require_admin_message_strict(message, session) is None:
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
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        stream = await create_stream(session, message.text.strip())
        group = await set_group_stream(session, group_id, stream.id)
        await session.commit()
    await state.clear()
    await message.answer(
        f"Для группы «{group.name}» задан поток «{stream.name}»."
    )


async def _weekly_slot_pick_rows(
    session: AsyncSession, *, group_id: str | None = None
) -> list[PickRow]:
    conditions = [
        WeeklySlot.active.is_(True),
        Group.archived_at.is_(None),
    ]
    if group_id is not None:
        conditions.append(WeeklySlot.group_id == group_id)
    rows = (
        await session.execute(
            select(WeeklySlot, Group)
            .join(Group, WeeklySlot.group_id == Group.id)
            .where(*conditions)
            .order_by(Group.name, WeeklySlot.weekday, WeeklySlot.start_time)
        )
    ).all()
    pick_rows: list[PickRow] = []
    for slot, group in rows:
        detail = (
            f"{WEEKDAY_LABELS[slot.weekday]} "
            f"{slot.start_time.strftime('%H:%M')}, {slot.place}"
        )
        if slot.notes:
            detail = f"{detail} ({slot.notes})"
        pick_rows.append(PickRow(id=slot.id, full_name=group.name, detail=detail))
    return pick_rows


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
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        pick_rows = await _weekly_slot_pick_rows(session)
    if not pick_rows:
        await query.answer("Нет недельных слотов", show_alert=True)
        return
    await state.update_data(mode="edit_all")
    await query.answer()
    if query.message:
        await query.message.answer("Какой слот изменить на все недели?")
        await _start_list_pick(
            query.message,
            state,
            purpose="slot_week",
            rows=pick_rows,
            title="Номер откроет этот слот.",
            hint=HINT_INDEX,
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
    await message.answer(
        f"Занятие {_format_user_session_date(on_date)}:", reply_markup=kb
    )


@router.callback_query(F.data == "once_cancel")
async def cb_once_cancel(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if (
        query.from_user is None
        or query.bot is None
        or "slot_id" not in data
        or "once_date" not in data
    ):
        await query.answer("Начните заново")
        return
    on_date = parse_dmy(data["once_date"])
    async with async_session_maker() as session:
        if await _require_admin_callback(query, session) is None:
            return
        slot = await session.get(WeeklySlot, data["slot_id"])
        school_session = await cancel_occurrence(session, data["slot_id"], on_date)
        group_name = await _session_group_name(session, slot.group_id)
        audience = await notice_audience(session, school_session.id)
        text = cancel_notice_text(
            group_name, school_session.starts_at, school_session.place
        )
        await session.commit()
    report, _, _ = await _send_notice(query.bot, audience, text)
    await state.clear()
    await query.answer("Отменено")
    if query.message:
        await query.message.answer(f"{text}\n\n{report}")


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
    audience: list[Person] | None = None
    notice_text: str | None = None
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        if mode == "edit_one":
            on_date = parse_dmy(data["once_date"])
            slot = await session.get(WeeklySlot, data["slot_id"])
            old_session = await ensure_occurrence(session, slot, on_date)
            old_starts = old_session.starts_at
            old_place = old_session.place
            group_name = await _session_group_name(session, slot.group_id)
            audience = await notice_audience(session, old_session.id)
            updated = await override_occurrence(
                session, data["slot_id"], on_date, start, end, data["slot_place"], notes or None
            )
            notice_text = reschedule_notice_text(
                group_name, old_starts, old_place, updated.starts_at, updated.place
            )
            text = notice_text
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
    if mode == "edit_one" and message.bot is not None and audience is not None and notice_text is not None:
        report, _, _ = await _send_notice(message.bot, audience, notice_text)
        text = f"{notice_text}\n\n{report}"
    await message.answer(text)
    await state.clear()
