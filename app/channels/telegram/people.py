"""Telegram people domain: clients, admins, contacts."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import (
    Attendance,
    ContactCard,
    Group,
    GroupMembership,
    Payment,
    Person,
    Product,
    SchoolSession,
    Subscription,
)
from app.services.attendance import (
    find_sessions_at,
    mark_attendance,
    unmark_attendance,
)
from app.services.billing import (
    list_payable_subscriptions,
    person_has_unpaid_started,
    record_payment,
    replace_partner,
    subscription_payment_status,
)
from app.services.contacts import (
    archive_contact,
    contact_for_person,
    create_contact,
    list_contacts,
    restore_contact,
    update_contact,
)
from app.services.school_time import (
    format_school_date,
    format_school_time,
    school_tz,
)
from app.services.user_labels import format_person_list_detail, format_person_role_label
from app.services.identity import demote_admin, invite_admin
from app.services.pick_list import (
    HINT_INDEX,
    HINT_ONE,
    PickError,
    PickRow,
    has_next_page,
    page_text,
    resolve_picks,
    short_report,
)
from app.services.roster import (
    archive_person,
    end_group_membership,
    parse_roster_line,
    restore_person,
    update_person_card,
)
from app.services.schedule import (
    active_group_membership,
    assign_group,
    create_adhoc_session,
)

from .common import (
    BTN_CONTACTS,
    BTN_PEOPLE,
    PickSpec,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _require_admin_callback,
    _require_admin_message,
    _require_admin_message_strict,
    _start_list_pick,
    _today_local,
    register_pick_spec,
)
from .states import (
    AdminInviteFSM,
    ContactEditFSM,
    ContactFSM,
    PayInFSM,
    PeopleAddFSM,
    PeoplePickFSM,
    PersonVisitFSM,
)

_VISIT_CANCEL = "отмена"
_PROMPT_VISIT_DATE = "Дата занятия (ДД.ММ.ГГГГ):"
_PROMPT_VISIT_TIME = "Время начала (ЧЧ:ММ):"
_DEFAULT_ADHOC_DURATION_MIN = 55
_UNLINKED_NOTE = "Ещё не в боте — оповещения не шлём."

logger = logging.getLogger(__name__)

router = Router(name="telegram.people")

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())


async def _active_membership(
    session: AsyncSession, person_id: str
) -> GroupMembership | None:
    return await active_group_membership(session, person_id)


_HOMONYM_PICK_ERROR = "Несколько людей с таким именем — отправьте номер из списка."

_WEEKDAY_FULL_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_PAIR_ERROR_RU = {
    "subscription product is not for couples": "не парный абонемент",
    "partner must differ from the primary person": "партнёр должен отличаться",
    "person already has an active couple subscription": "у человека уже есть пара",
}


def _pair_error_message(exc: ValueError) -> str:
    return _PAIR_ERROR_RU.get(str(exc), "Не удалось изменить пару.")


def _group_visit_name(group: Group | None) -> str:
    if group is None:
        return "?"
    if group.archived_at is not None:
        return f"{group.name} (архив)"
    return group.name


def _session_visit_line(
    session: SchoolSession, group_name: str | None = None
) -> str:
    if session.session_date is not None:
        day = session.session_date
    else:
        starts = session.starts_at
        if starts.tzinfo is None:
            day = starts.replace(tzinfo=school_tz()).date()
        else:
            day = starts.astimezone(school_tz()).date()
    weekday = _WEEKDAY_FULL_RU[day.weekday()]
    line = (
        f"{weekday}, {format_school_date(day)}, "
        f"{format_school_time(session.starts_at)}"
    )
    if group_name:
        line = f"{line}, {group_name}"
    return line


def _parse_visit_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d.%m.%Y").date()


def _parse_visit_time(text: str) -> time:
    return datetime.strptime(text.strip(), "%H:%M").time()


def _visits_keyboard(person_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить",
                    callback_data=f"ppl_visit_add:{person_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Изменить",
                    callback_data=f"ppl_visit_edit:{person_id}",
                )
            ],
        ]
    )


def _visit_confirm_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data=f"ppl_visit_yes:{session_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="ppl_visit_cancel",
                )
            ],
        ]
    )


def _visit_create_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать занятие",
                    callback_data="ppl_visit_create",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="ppl_visit_cancel",
                )
            ],
        ]
    )


def _visit_create_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data="ppl_visit_create_yes",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="ppl_visit_cancel",
                )
            ],
        ]
    )


def _visit_edit_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перенести",
                    callback_data="ppl_visit_move",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data="ppl_visit_del",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="ppl_visit_cancel",
                )
            ],
        ]
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


def _is_client_like(person: Person, membership: GroupMembership | None) -> bool:
    return person.role == "client" or (
        person.role == "admin" and membership is not None
    )


async def _person_group_detail(session: AsyncSession, person_id: str) -> str:
    membership = await _active_membership(session, person_id)
    if membership is None:
        return "без группы"
    group = await session.get(Group, membership.group_id)
    return group.name if group is not None else "без группы"


async def _person_pick_detail(
    session: AsyncSession, person: Person, today: date
) -> str:
    membership = await _active_membership(session, person.id)
    group_name = await _person_group_detail(session, person.id)
    role_label = format_person_role_label(
        person.role, membership is not None
    )
    unpaid = await person_has_unpaid_started(session, person.id, today)
    return format_person_list_detail(group_name, role_label, unpaid)


def _people_pick_shows_group(purpose: str) -> bool:
    return purpose in ("assign", "card", "leave")


async def _pick_rows_for_ids(
    session: AsyncSession,
    pick_ids: list[str],
    *,
    with_group_detail: bool,
    unpaid_only: bool = False,
) -> list[PickRow]:
    if not pick_ids:
        return []
    result = await session.execute(select(Person).where(Person.id.in_(pick_ids)))
    by_id = {p.id: p for p in result.scalars().all()}
    today = _today_local()
    rows: list[PickRow] = []
    for person_id in pick_ids:
        person = by_id.get(person_id)
        if person is None:
            continue
        if with_group_detail:
            detail = await _person_pick_detail(session, person, today)
        elif unpaid_only and await person_has_unpaid_started(session, person.id, today):
            detail = "не оплачен"
        else:
            detail = ""
        rows.append(PickRow(id=person.id, full_name=person.full_name, detail=detail))
    return rows


async def _list_all_people_pick_rows(session: AsyncSession) -> list[PickRow]:
    people = list(
        (
            await session.execute(
                select(Person)
                .where(Person.archived_at.is_(None))
                .order_by(Person.full_name)
            )
        ).scalars().all()
    )
    today = _today_local()
    rows: list[PickRow] = []
    for person in people:
        unpaid = await person_has_unpaid_started(session, person.id, today)
        detail = "не оплачен" if unpaid else ""
        rows.append(PickRow(id=person.id, full_name=person.full_name, detail=detail))
    return rows


async def _list_group_client_pick_rows(
    session: AsyncSession,
    group_id: str,
    *,
    exclude_person_id: str | None = None,
) -> list[PickRow]:
    result = await session.execute(
        select(Person)
        .join(GroupMembership, GroupMembership.person_id == Person.id)
        .where(
            GroupMembership.group_id == group_id,
            GroupMembership.ended_on.is_(None),
            Person.role == "client",
            Person.archived_at.is_(None),
        )
        .order_by(Person.full_name)
    )
    rows: list[PickRow] = []
    for person in result.scalars().all():
        if exclude_person_id is not None and person.id == exclude_person_id:
            continue
        rows.append(PickRow(id=person.id, full_name=person.full_name))
    return rows


async def _list_non_admin_pick_rows(session: AsyncSession) -> list[PickRow]:
    people = list(
        (
            await session.execute(
                select(Person)
                .where(
                    Person.role != "admin",
                    Person.archived_at.is_(None),
                )
                .order_by(Person.full_name)
            )
        ).scalars().all()
    )
    rows: list[PickRow] = []
    for person in people:
        detail = await _person_group_detail(session, person.id)
        rows.append(PickRow(id=person.id, full_name=person.full_name, detail=detail))
    return rows


async def _list_client_pick_rows(session: AsyncSession) -> list[PickRow]:
    clients = list(
        (
            await session.execute(
                select(Person)
                .where(
                    Person.role == "client",
                    Person.archived_at.is_(None),
                )
                .order_by(Person.full_name)
            )
        ).scalars().all()
    )
    return [PickRow(id=c.id, full_name=c.full_name) for c in clients]


async def _list_active_client_pick_rows(session: AsyncSession) -> list[PickRow]:
    return await _list_client_pick_rows(session)


async def _list_archived_client_rows(session: AsyncSession) -> list[Person]:
    return list(
        (
            await session.execute(
                select(Person)
                .where(
                    Person.role == "client",
                    Person.archived_at.is_not(None),
                )
                .order_by(Person.full_name)
            )
        ).scalars().all()
    )


async def _person_card_keyboard(
    session: AsyncSession, person_id: str
) -> InlineKeyboardMarkup | None:
    person = await session.get(Person, person_id)
    if person is None or person.archived_at is not None:
        return None
    membership = await _active_membership(session, person_id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="В группу",
                callback_data=f"ppl_to_grp:{person_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Из группы",
                callback_data=f"ppl_from_grp:{person_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Изменить карточку",
                callback_data=f"ppl_edit:{person_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Убрать в архив",
                callback_data=f"ppl_arch_ask:{person_id}",
            )
        ],
    ]
    if _is_client_like(person, membership):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Абонементы",
                    callback_data=f"ppl_subs:{person_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Посещения",
                    callback_data=f"ppl_visits:{person_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Оплаты",
                    callback_data=f"ppl_pays:{person_id}",
                )
            ]
        )
    if person.role == "admin":
        rows.append(
            [
                InlineKeyboardButton(
                    text="Контакт для клиентов",
                    callback_data=f"ppl_contact:{person_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _person_card_text(session: AsyncSession, person_id: str) -> str | None:
    person = await session.get(Person, person_id)
    if person is None or person.archived_at is not None:
        return None
    membership = await _active_membership(session, person_id)
    role_label = format_person_role_label(person.role, membership is not None)
    lines = [f"{person.full_name} — {role_label}"]
    if person.phone:
        lines.append(f"Телефон: {person.phone}")
    detail = await _person_group_detail(session, person_id)
    lines.append(f"Группа: {detail}")
    return "\n".join(lines)


async def send_person_card(message: Message, person_id: str) -> None:
    async with async_session_maker() as session:
        text = await _person_card_text(session, person_id)
        if text is None:
            await message.answer("Не найдено")
            return
        keyboard = await _person_card_keyboard(session, person_id)
    if keyboard is None:
        await message.answer("Не найдено")
        return
    await message.answer(text, reply_markup=keyboard)


async def _send_person_card(message: Message, person_id: str) -> None:
    await send_person_card(message, person_id)


async def _prompt_leave_person(
    message: Message, state: FSMContext, person_id: str
) -> None:
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        membership = await _active_membership(session, person_id)
        if person is None:
            await state.clear()
            await message.answer("Не найдено")
            return
        if membership is None:
            await state.clear()
            await message.answer(f"{person.full_name} сейчас не в группе.")
            return
        group = await session.get(Group, membership.group_id)
        group_name = group.name if group is not None else "?"
    await state.update_data(leave_person_id=person_id, leave_group_name=group_name)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать",
                    callback_data=f"ppl_leave_go:{person_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="ppl_leave_cancel")],
        ]
    )
    await message.answer(
        f"Убрать {person.full_name} из «{group_name}»?",
        reply_markup=kb,
    )


def _people_pick_keyboard(rows: list[PickRow], page: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if has_next_page(rows, page):
        buttons.append(
            [InlineKeyboardButton(text="Дальше", callback_data="pp_more")]
        )
    buttons.append(
        [InlineKeyboardButton(text="Отмена", callback_data="pp_cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_people_pick_page(
    message: Message,
    rows: list[PickRow],
    page: int,
    *,
    title: str | None = None,
) -> None:
    body = page_text(rows, page, HINT_ONE)
    text = f"{title}\n\n{body}" if title else body
    await message.answer(text, reply_markup=_people_pick_keyboard(rows, page))


async def _start_people_pick(
    message: Message,
    state: FSMContext,
    *,
    purpose: str,
    rows: list[PickRow],
    title: str,
) -> None:
    await state.set_state(PeoplePickFSM.waiting)
    await state.update_data(
        pick_ids=[row.id for row in rows],
        pick_page=0,
        pick_purpose=purpose,
    )
    await _send_people_pick_page(message, rows, 0, title=title)

async def _apply_ppl_add_choice(
    message: Message, state: FSMContext, group_id: str
) -> None:
    data = await state.get_data()
    full_name = data.get("add_full_name")
    name_key = data.get("add_name_key")
    if not full_name or not name_key:
        await state.clear()
        await message.answer("Сначала укажите фамилию и имя.")
        return
    today = _today_local()
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.archived_at is not None:
            await message.answer("Не найдено")
            return
        group_name = group.name
        result = await session.execute(
            select(Person).where(
                Person.name_key == name_key,
                Person.telegram_user_id.is_(None),
                Person.archived_at.is_(None),
            )
        )
        person = result.scalar_one_or_none()
        if person is not None:
            membership = await _active_membership(session, person.id)
            if membership is not None:
                in_group = await session.get(Group, membership.group_id)
                in_name = in_group.name if in_group is not None else "?"
                await state.clear()
                await message.answer(
                    f"{person.full_name} уже в группе «{in_name}»."
                )
                return
            await assign_group(session, person.id, group_id, today)
            report_name = person.full_name
        else:
            person = Person(
                full_name=str(full_name),
                name_key=str(name_key),
                role="client",
                telegram_user_id=None,
            )
            session.add(person)
            await session.flush()
            await assign_group(session, person.id, group_id, today)
            report_name = person.full_name
        await session.commit()
    await state.clear()
    await message.answer(f"Добавлен(а) {report_name} в группу «{group_name}».")


async def _format_contacts(session: AsyncSession) -> str:
    cards = await list_contacts(session, archived=False)
    if not cards:
        return "Контакты пока не добавлены."
    lines = ["Контакты:"]
    for card in cards:
        lines.append(f"• {card.name} — {card.phone} ({card.role_label})")
    return "\n".join(lines)


def _contact_card_text(card: ContactCard) -> str:
    return f"{card.name}\n{card.phone}\n{card.role_label}"


async def _admins_menu_keyboard(session: AsyncSession) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Добавить админа", callback_data="admin_invite")],
        [InlineKeyboardButton(text="Новый контакт", callback_data="admin_contact")],
    ]
    admins = list(
        (
            await session.execute(
                select(Person).where(Person.role == "admin").order_by(Person.full_name)
            )
        ).scalars().all()
    )
    for admin in admins:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Снять: {admin.full_name}",
                    callback_data=f"adm_ask:{admin.id}",
                )
            ]
        )
    for card in await list_contacts(session, archived=False):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Контакт: {card.name}",
                    callback_data=f"ctc_card:{card.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Архив контактов", callback_data="ctc_arch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _demote_admin_error(exc: ValueError) -> str:
    msg = str(exc)
    if msg == "Cannot demote yourself":
        return "нельзя снять себя"
    if msg == "Cannot demote the last admin":
        return "нельзя снять последнего админа"
    return "Не удалось снять админа."


def _format_admin_contacts_list(contacts: list) -> str:
    lines = ["Свяжитесь с администратором, чтобы вас записали:"]
    for contact in contacts:
        label = f"@{contact.username}" if contact.username else contact.full_name
        if contact.phone:
            label = f"{label}, {contact.phone}"
        lines.append(f"• {label}")
    return "\n".join(lines)


async def _on_index_pick_ppl_add(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _apply_ppl_add_choice(message, state, picked[0].id)


@router.callback_query(F.data == "ppl_card")
async def cb_people_card(query: CallbackQuery, state: FSMContext) -> None:
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
            purpose="card",
            rows=rows,
            title="Откроется карточка клиента по номеру.",
        )


@router.callback_query(F.data == "ppl_leave")
async def cb_people_leave(query: CallbackQuery, state: FSMContext) -> None:
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
            purpose="leave",
            rows=rows,
            title="Удаление из группы по номеру.",
        )
@router.callback_query(F.data == "pp_cancel")
async def cb_people_pick_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer("Выбор отменён.")


@router.callback_query(F.data == "pp_more")
async def cb_people_pick_more(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    pick_ids: list[str] = list(data.get("pick_ids") or [])
    page = int(data.get("pick_page") or 0) + 1
    purpose = str(data.get("pick_purpose") or "")
    with_group_detail = _people_pick_shows_group(purpose)
    async with async_session_maker() as session:
        rows = await _pick_rows_for_ids(
            session,
            pick_ids,
            with_group_detail=with_group_detail,
            unpaid_only=purpose == "view",
        )
    if not rows or page >= (len(rows) + 19) // 20:
        await query.answer("Больше нет")
        return
    await state.update_data(pick_page=page)
    await query.answer()
    if query.message:
        await query.message.answer(
            page_text(rows, page, HINT_ONE),
            reply_markup=_people_pick_keyboard(rows, page),
        )


@router.callback_query(F.data.startswith("ppl_leave_go:"))
async def cb_people_leave_confirm(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    today = _today_local()
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        membership = await end_group_membership(session, person_id, today)
        group_name = "?"
        if membership is not None:
            group = await session.get(Group, membership.group_id)
            if group is not None:
                group_name = group.name
        await session.commit()
        if person is None or membership is None:
            await state.clear()
            await query.answer("Не в группе", show_alert=True)
            return
        report = short_report(
            f"{person.full_name} выведена из «{group_name}»",
            [],
        )
    await state.clear()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(report)


@router.callback_query(F.data == "ppl_leave_cancel")
async def cb_people_leave_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.clear()
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Удаление из группы отменено.")


@router.callback_query(F.data == "ppl_list")
async def cb_ppl_list(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        rows = await _list_all_people_pick_rows(session)
    if not rows:
        await query.answer("Нет людей", show_alert=True)
        return
    await query.answer()
    if query.message:
        await _start_people_pick(
            query.message,
            state,
            purpose="view",
            rows=rows,
            title="Номер откроет карточку.",
        )


@router.callback_query(F.data == "ppl_add")
async def cb_ppl_add(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(PeopleAddFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Фамилия и имя одной строкой.")


@router.callback_query(F.data == "ppl_arch")
async def cb_ppl_arch(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        archived = await _list_archived_client_rows(session)
    if not archived:
        await query.answer("Архив пуст", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Вернуть: {p.full_name}",
                    callback_data=f"ppl_back:{p.id}",
                )
            ]
            for p in archived
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Архив клиентов:", reply_markup=kb)


@router.callback_query(F.data.startswith("ppl_back:"))
async def cb_ppl_restore(query: CallbackQuery) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None or person.archived_at is None:
            await query.answer("Не найдено", show_alert=True)
            return
        await restore_person(session, person_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer("Клиент возвращён")


@router.callback_query(F.data.startswith("ppl_arch_ask:"))
async def cb_ppl_arch_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None or person.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        name = person.full_name
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать",
                    callback_data=f"ppl_arch_yes:{person_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="ppl_arch_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(f"Убрать {name}?", reply_markup=kb)


@router.callback_query(F.data == "ppl_arch_cancel")
async def cb_ppl_arch_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Без изменений.")


@router.callback_query(F.data.startswith("ppl_arch_yes:"))
async def cb_ppl_arch_yes(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    now = datetime.now(UTC)
    today = _today_local()
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None or person.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        if person.role == "admin":
            await query.answer("Нельзя убрать администратора", show_alert=True)
            return
        name = person.full_name
        await archive_person(session, person_id, now, today)
        await session.commit()
    await state.clear()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(f"{name} в архиве")


@router.callback_query(F.data.startswith("ppl_to_grp:"))
async def cb_ppl_to_grp(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await query.answer()
    if query.message:
        from .groups import _prompt_assign_group

        await _prompt_assign_group(query.message, state, person_id)


@router.callback_query(F.data.startswith("ppl_from_grp:"))
async def cb_ppl_from_grp(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await query.answer()
    if query.message:
        await _prompt_leave_person(query.message, state, person_id)


async def _format_person_subscriptions(
    session: AsyncSession, person_id: str
) -> tuple[str, InlineKeyboardMarkup | None]:
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
    subs = list(result.scalars().all())
    if not subs:
        return "Абонементов нет.", None
    lines: list[str] = ["Абонементы:"]
    buttons: list[list[InlineKeyboardButton]] = []
    for sub in subs:
        product = await session.get(Product, sub.product_id)
        product_name = product.name if product is not None else "?"
        partner_line = ""
        if sub.partner_id:
            partner = await session.get(Person, sub.partner_id)
            if partner is not None:
                partner_line = f"\nПартнёр: {partner.full_name}"
        period = _subscription_period_label(sub)
        status = subscription_payment_status(sub)
        unpaid = " — не оплачен" if status == "unpaid" else ""
        lines.append(
            f"\n{product_name}{partner_line}\n"
            f"{period}\n"
            f"Осталось занятий: {sub.lessons_left}{unpaid}"
        )
        if product is not None and product.audience == "couple":
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Изменить пару",
                        callback_data=f"ppl_pair:{sub.id}",
                    )
                ]
            )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(lines), kb


async def _person_visit_rows(
    session: AsyncSession, person_id: str
) -> list[tuple[Attendance, SchoolSession, str]]:
    result = await session.execute(
        select(Attendance, SchoolSession, Group)
        .join(SchoolSession, Attendance.session_id == SchoolSession.id)
        .join(Group, SchoolSession.group_id == Group.id)
        .where(Attendance.person_id == person_id)
        .order_by(desc(SchoolSession.starts_at))
    )
    return [
        (att, school_session, group.name)
        for att, school_session, group in result.all()
    ]


async def _format_person_visits(session: AsyncSession, person_id: str) -> str:
    rows = await _person_visit_rows(session, person_id)
    if not rows:
        return "Посещений нет."
    lines = ["Посещения:"]
    for _att, school_session, group_name in rows:
        lines.append(f"• {_session_visit_line(school_session, group_name)}")
    return "\n".join(lines)


async def _format_person_visits_numbered(
    session: AsyncSession, person_id: str
) -> tuple[str, list[str]]:
    rows = await _person_visit_rows(session, person_id)
    if not rows:
        return "Посещений нет.", []
    session_ids = [school_session.id for _att, school_session, _g in rows]
    lines = ["Посещения (номер для изменения):"]
    for index, (_att, school_session, group_name) in enumerate(rows, start=1):
        lines.append(
            f"{index}. {_session_visit_line(school_session, group_name)}"
        )
    return "\n".join(lines), session_ids


async def _session_label(session: AsyncSession, school_session_id: str) -> str:
    school_session = await session.get(SchoolSession, school_session_id)
    if school_session is None:
        return "занятие"
    group = await session.get(Group, school_session.group_id)
    return _session_visit_line(school_session, _group_visit_name(group))


async def _clear_visit_cancel(message: Message, state: FSMContext) -> bool:
    if message.text and message.text.strip().lower() == _VISIT_CANCEL:
        await state.clear()
        await message.answer("Отменено.")
        return True
    return False


async def _ask_visit_confirm(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    school_session_id: str,
) -> None:
    label = await _session_label(session, school_session_id)
    data = await state.get_data()
    mode = data.get("visit_mode", "add")
    if mode == "move":
        prompt = f"Перенести на: {label}?"
    else:
        prompt = f"Отметить: {label}?"
    await message.answer(prompt, reply_markup=_visit_confirm_keyboard(school_session_id))


async def _resolve_visit_sessions(
    message: Message,
    state: FSMContext,
    on_date: date,
    at_time: time,
) -> None:
    await state.update_data(
        visit_date=on_date.isoformat(),
        visit_time=at_time.strftime("%H:%M"),
    )
    data = await state.get_data()
    mode = str(data.get("visit_mode") or "add")
    async with async_session_maker() as session:
        matches = await find_sessions_at(session, on_date, at_time)
        # Materialize may create SchoolSession rows — persist before confirm /
        # mark in a later request, otherwise ids vanish on rollback.
        await session.commit()
        if not matches:
            if mode == "add":
                await message.answer(
                    "Занятие не найдено. Создать разовое и отметить?",
                    reply_markup=_visit_create_offer_keyboard(),
                )
            else:
                await message.answer(
                    "Занятие не найдено. Введите дату ещё раз или «отмена»."
                )
                await state.set_state(PersonVisitFSM.date)
            return
        if len(matches) == 1:
            await _ask_visit_confirm(message, state, session, matches[0].id)
            return
        lines = ["Несколько занятий, отправьте номер:"]
        ids: list[str] = []
        for index, school_session in enumerate(matches, start=1):
            group = await session.get(Group, school_session.group_id)
            lines.append(
                f"{index}. {_session_visit_line(school_session, _group_visit_name(group))}"
            )
            ids.append(school_session.id)
        await state.update_data(visit_pick_ids=ids)
        await state.set_state(PersonVisitFSM.pick)
        await message.answer("\n".join(lines))


async def _unlinked_suffix(session: AsyncSession, person_id: str) -> str:
    person = await session.get(Person, person_id)
    if person is None or person.telegram_user_id is not None:
        return ""
    return f"\n{_UNLINKED_NOTE}"


async def _begin_visit_create(
    message: Message,
    state: FSMContext,
    *,
    group_id: str | None = None,
) -> None:
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    if not person_id:
        await state.clear()
        await message.answer("Начните снова.")
        return
    async with async_session_maker() as session:
        resolved_group_id = group_id
        if resolved_group_id is None:
            membership = await _active_membership(session, str(person_id))
            if membership is not None:
                resolved_group_id = membership.group_id
        if resolved_group_id is None:
            from .groups import _list_active_groups

            groups = await _list_active_groups(session)
            if not groups:
                await message.answer("Нет активных групп — создать занятие нельзя.")
                await state.clear()
                return
            if len(groups) == 1:
                resolved_group_id = groups[0].id
            else:
                pick_rows = [
                    PickRow(id=group.id, full_name=group.name) for group in groups
                ]
                await _start_list_pick(
                    message,
                    state,
                    purpose="visit_create_grp",
                    rows=pick_rows,
                    title="Группа для нового занятия:",
                    hint=HINT_INDEX,
                )
                return
        group = await session.get(Group, resolved_group_id)
        gname = group.name if group is not None else "?"
    await state.update_data(visit_create_group_id=resolved_group_id)
    await state.set_state(PersonVisitFSM.create_place)
    await message.answer(
        f"Группа: {gname}.\nМесто занятия. Или «отмена»."
    )


async def _apply_visit_confirm(
    message: Message,
    state: FSMContext,
    *,
    admin_id: str,
    person_id: str,
    school_session_id: str,
    mode: str,
    old_session_id: str | None,
) -> None:
    async with async_session_maker() as session:
        try:
            if mode == "move":
                if not old_session_id:
                    await message.answer("Не удалось перенести.")
                    await state.clear()
                    return
                if old_session_id == school_session_id:
                    await message.answer("Это то же занятие.")
                    await state.clear()
                    return
                existing_new = await session.execute(
                    select(Attendance).where(
                        Attendance.person_id == person_id,
                        Attendance.session_id == school_session_id,
                    )
                )
                if existing_new.scalar_one_or_none() is not None:
                    await message.answer("На новом занятии уже есть отметка.")
                    await state.clear()
                    return
                removed = await unmark_attendance(
                    session, person_id, old_session_id
                )
                if removed is None:
                    await message.answer("Старое посещение не найдено.")
                    await state.clear()
                    return
            await mark_attendance(
                session,
                person_id,
                school_session_id,
                "admin",
                marked_by_id=admin_id,
            )
            await session.commit()
            text = await _format_person_visits(session, person_id)
            note = await _unlinked_suffix(session, person_id)
        except ValueError as exc:
            await session.rollback()
            err = str(exc)
            if "not scheduled" in err:
                await message.answer("Занятие отменено.")
            elif "no active drop_in" in err or "no drop_in price" in err:
                await message.answer(
                    "Не удалось отметить: нет активного разового тарифа на эту дату."
                )
            else:
                await message.answer("Не удалось отметить посещение.")
            await state.clear()
            return
        except Exception:
            await session.rollback()
            logger.exception("Visit confirm failed")
            await message.answer("Не удалось отметить посещение.")
            await state.clear()
            return
    await state.clear()
    verb = "Перенесено." if mode == "move" else "Отмечено."
    await message.answer(
        f"{verb}{note}\n\n{text}",
        reply_markup=_visits_keyboard(person_id),
    )


def _parse_payment_amount(text: str) -> Decimal:
    return Decimal(text.strip().replace(",", "."))


def _payments_keyboard(person_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Внести оплату",
                    callback_data=f"ppl_payin:{person_id}",
                )
            ]
        ]
    )


async def _payin_amount_prompt(session: AsyncSession, sub: Subscription) -> str:
    product = await session.get(Product, sub.product_id)
    name = product.name if product is not None else "?"
    remainder = sub.amount_due - sub.amount_paid
    return (
        f"{name}\n"
        f"Уже оплачено: {sub.amount_paid}\n"
        f"Осталось: {remainder}\n"
        "Сумма оплаты (число, запятая или точка):"
    )


async def _format_person_payments(session: AsyncSession, person_id: str) -> str:
    result = await session.execute(
        select(Payment, Subscription, Product)
        .join(Subscription, Payment.subscription_id == Subscription.id)
        .join(Product, Subscription.product_id == Product.id)
        .where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            )
        )
        .order_by(desc(Payment.paid_on), desc(Payment.created_at))
    )
    rows = result.all()
    if not rows:
        return "Оплат нет."
    lines = ["Оплаты:"]
    for payment, _sub, product in rows:
        lines.append(
            f"• {format_school_date(payment.paid_on)} — "
            f"{payment.amount} — {product.name}"
        )
    return "\n".join(lines)


@router.callback_query(F.data.startswith("ppl_subs:"))
async def cb_ppl_subs(query: CallbackQuery) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        text, kb = await _format_person_subscriptions(session, person_id)
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ppl_visits:"))
async def cb_ppl_visits(query: CallbackQuery) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        text = await _format_person_visits(session, person_id)
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=_visits_keyboard(person_id))


@router.callback_query(F.data.startswith("ppl_visit_add:"))
async def cb_ppl_visit_add(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await state.clear()
    await state.update_data(visit_mode="add", visit_person_id=person_id)
    await state.set_state(PersonVisitFSM.date)
    await query.answer()
    if query.message:
        await query.message.answer(
            f"{_PROMPT_VISIT_DATE}\nИли «отмена»."
        )


@router.callback_query(F.data.startswith("ppl_visit_edit:"))
async def cb_ppl_visit_edit(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        text, session_ids = await _format_person_visits_numbered(session, person_id)
    await query.answer()
    if not session_ids:
        if query.message:
            await query.message.answer(text, reply_markup=_visits_keyboard(person_id))
        return
    await state.clear()
    await state.update_data(
        visit_mode="edit",
        visit_person_id=person_id,
        visit_edit_ids=session_ids,
    )
    await state.set_state(PersonVisitFSM.edit_index)
    if query.message:
        await query.message.answer(f"{text}\n\nНомер или «отмена».")


@router.callback_query(F.data == "ppl_visit_cancel")
async def cb_ppl_visit_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    await state.clear()
    await query.answer()
    if query.message:
        if person_id:
            async with async_session_maker() as session:
                text = await _format_person_visits(session, str(person_id))
            await query.message.answer(
                f"Отменено.\n\n{text}",
                reply_markup=_visits_keyboard(str(person_id)),
            )
        else:
            await query.message.answer("Отменено.")


@router.callback_query(F.data.startswith("ppl_visit_yes:"))
async def cb_ppl_visit_yes(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None or query.from_user is None:
        return
    school_session_id = query.data.split(":", 1)[1]
    admin = await _require_admin_callback(query)
    if admin is None:
        return
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    if not person_id:
        await state.clear()
        await query.answer()
        return
    mode = str(data.get("visit_mode") or "add")
    old_session_id = data.get("visit_old_session_id")
    await query.answer()
    if query.message is None:
        return
    await _apply_visit_confirm(
        query.message,
        state,
        admin_id=admin.id,
        person_id=str(person_id),
        school_session_id=school_session_id,
        mode=mode if mode in ("add", "move") else "add",
        old_session_id=str(old_session_id) if old_session_id else None,
    )


@router.callback_query(F.data == "ppl_visit_del")
async def cb_ppl_visit_del(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    old_session_id = data.get("visit_old_session_id")
    if not person_id or not old_session_id:
        await state.clear()
        await query.answer()
        return
    async with async_session_maker() as session:
        removed = await unmark_attendance(
            session, str(person_id), str(old_session_id)
        )
        if removed is None:
            await session.rollback()
            await state.clear()
            await query.answer()
            if query.message:
                await query.message.answer("Посещение не найдено.")
            return
        await session.commit()
        text = await _format_person_visits(session, str(person_id))
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Удалено.\n\n{text}",
            reply_markup=_visits_keyboard(str(person_id)),
        )


@router.callback_query(F.data == "ppl_visit_create")
async def cb_ppl_visit_create(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    if not data.get("visit_person_id") or not data.get("visit_date") or not data.get(
        "visit_time"
    ):
        await state.clear()
        await query.answer()
        if query.message:
            await query.message.answer("Начните снова.")
        return
    mode = str(data.get("visit_mode") or "add")
    if mode != "add":
        await query.answer("Создание только при добавлении", show_alert=True)
        return
    await query.answer()
    if query.message is None:
        return
    await _begin_visit_create(query.message, state)


@router.message(PersonVisitFSM.create_place)
async def fsm_visit_create_place(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    place = message.text.strip()
    if not place:
        await message.answer("Укажите место.")
        return
    await state.update_data(visit_create_place=place)
    await state.set_state(PersonVisitFSM.create_duration)
    await message.answer(
        f"Длительность в минутах (по умолчанию {_DEFAULT_ADHOC_DURATION_MIN}). "
        "Число или «отмена»."
    )


@router.message(PersonVisitFSM.create_duration)
async def fsm_visit_create_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    raw = message.text.strip()
    if raw == "" or raw == "—":
        minutes = _DEFAULT_ADHOC_DURATION_MIN
    else:
        try:
            minutes = int(raw)
        except ValueError:
            await message.answer("Введите число минут.")
            return
    if minutes <= 0:
        await message.answer("Длительность должна быть больше нуля.")
        return
    data = await state.get_data()
    group_id = data.get("visit_create_group_id")
    place = data.get("visit_create_place")
    raw_date = data.get("visit_date")
    raw_time = data.get("visit_time")
    if not group_id or not place or not raw_date or not raw_time:
        await state.clear()
        await message.answer("Начните снова.")
        return
    on_date = date.fromisoformat(str(raw_date))
    at_time = _parse_visit_time(str(raw_time))
    await state.update_data(visit_create_duration=str(minutes))
    async with async_session_maker() as session:
        group = await session.get(Group, str(group_id))
        gname = group.name if group is not None else "?"
        ends = (
            datetime.combine(on_date, at_time) + timedelta(minutes=minutes)
        ).time()
        label = (
            f"{_WEEKDAY_FULL_RU[on_date.weekday()]}, "
            f"{format_school_date(on_date)}, "
            f"{at_time.strftime('%H:%M')}–{ends.strftime('%H:%M')}, "
            f"{gname}, {place}"
        )
    await message.answer(
        f"Создать и отметить: {label}?",
        reply_markup=_visit_create_confirm_keyboard(),
    )


@router.callback_query(F.data == "ppl_visit_create_yes")
async def cb_ppl_visit_create_yes(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user is None:
        return
    admin = await _require_admin_callback(query)
    if admin is None:
        return
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    group_id = data.get("visit_create_group_id")
    place = data.get("visit_create_place")
    raw_date = data.get("visit_date")
    raw_time = data.get("visit_time")
    raw_duration = data.get("visit_create_duration") or str(_DEFAULT_ADHOC_DURATION_MIN)
    if not person_id or not group_id or not place or not raw_date or not raw_time:
        await state.clear()
        await query.answer()
        if query.message:
            await query.message.answer("Начните снова.")
        return
    on_date = date.fromisoformat(str(raw_date))
    at_time = _parse_visit_time(str(raw_time))
    minutes = int(str(raw_duration))
    await query.answer()
    if query.message is None:
        return
    async with async_session_maker() as session:
        try:
            school_session = await create_adhoc_session(
                session,
                group_id=str(group_id),
                on_date=on_date,
                start_time=at_time,
                duration_minutes=minutes,
                place=str(place),
            )
            await mark_attendance(
                session,
                str(person_id),
                school_session.id,
                "admin",
                marked_by_id=admin.id,
            )
            await session.commit()
            text = await _format_person_visits(session, str(person_id))
            note = await _unlinked_suffix(session, str(person_id))
        except ValueError as exc:
            await session.rollback()
            await state.clear()
            err = str(exc)
            if "no active drop_in" in err or "no drop_in price" in err:
                await query.message.answer(
                    "Занятие могло создаться, но отметить не удалось: "
                    "нет активного разового тарифа на эту дату."
                )
            elif "crosses midnight" in err:
                await query.message.answer(
                    "Длительность переходит через полночь — укажите меньше минут."
                )
            else:
                await query.message.answer(
                    "Не удалось создать занятие или отметить."
                )
            return
        except Exception:
            await session.rollback()
            await state.clear()
            logger.exception("Visit create+mark failed")
            await query.message.answer("Не удалось создать занятие или отметить.")
            return
    await state.clear()
    await query.message.answer(
        f"Создано и отмечено.{note}\n\n{text}",
        reply_markup=_visits_keyboard(str(person_id)),
    )


async def _on_index_pick_visit_create_grp(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _begin_visit_create(message, state, group_id=picked[0].id)


async def _build_rows_visit_create_grp(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    from .groups import _build_rows_active_groups

    return await _build_rows_active_groups(session, data)


register_pick_spec(
    "visit_create_grp",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_visit_create_grp,
        on_index_pick=_on_index_pick_visit_create_grp,
    ),
)


@router.callback_query(F.data == "ppl_visit_move")
async def cb_ppl_visit_move(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    person_id = data.get("visit_person_id")
    old_session_id = data.get("visit_old_session_id")
    if not person_id or not old_session_id:
        await state.clear()
        await query.answer()
        return
    await state.update_data(visit_mode="move")
    await state.set_state(PersonVisitFSM.date)
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Новая дата занятия.\n{_PROMPT_VISIT_DATE}\nИли «отмена»."
        )


@router.message(PersonVisitFSM.date)
async def fsm_visit_date(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    try:
        on_date = _parse_visit_date(message.text)
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ")
        return
    await state.update_data(visit_date=on_date.isoformat())
    await state.set_state(PersonVisitFSM.time)
    await message.answer(_PROMPT_VISIT_TIME)


@router.message(PersonVisitFSM.time)
async def fsm_visit_time(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    try:
        at_time = _parse_visit_time(message.text)
    except ValueError:
        await message.answer("Формат: ЧЧ:ММ")
        return
    data = await state.get_data()
    raw_date = data.get("visit_date")
    if not raw_date:
        await state.clear()
        return
    on_date = date.fromisoformat(str(raw_date))
    await _resolve_visit_sessions(message, state, on_date, at_time)


@router.message(PersonVisitFSM.pick)
async def fsm_visit_pick(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    data = await state.get_data()
    ids = data.get("visit_pick_ids") or []
    try:
        index = int(message.text.strip())
    except ValueError:
        await message.answer("Отправьте номер из списка.")
        return
    if index < 1 or index > len(ids):
        await message.answer("Номер вне списка.")
        return
    school_session_id = str(ids[index - 1])
    async with async_session_maker() as session:
        await _ask_visit_confirm(message, state, session, school_session_id)


@router.message(PersonVisitFSM.edit_index)
async def fsm_visit_edit_index(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if await _clear_visit_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    data = await state.get_data()
    ids = data.get("visit_edit_ids") or []
    try:
        index = int(message.text.strip())
    except ValueError:
        await message.answer("Отправьте номер из списка.")
        return
    if index < 1 or index > len(ids):
        await message.answer("Номер вне списка.")
        return
    old_session_id = str(ids[index - 1])
    await state.update_data(visit_old_session_id=old_session_id)
    async with async_session_maker() as session:
        label = await _session_label(session, old_session_id)
    await message.answer(
        f"{label}\nПеренести или удалить?",
        reply_markup=_visit_edit_action_keyboard(),
    )


@router.callback_query(F.data.startswith("ppl_pays:"))
async def cb_ppl_pays(query: CallbackQuery) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        text = await _format_person_payments(session, person_id)
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=_payments_keyboard(person_id))


@router.callback_query(F.data.startswith("ppl_payin:"))
async def cb_ppl_payin(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    today = datetime.now(school_tz()).date()
    async with async_session_maker() as session:
        subs = await list_payable_subscriptions(session, person_id, today)
    if not subs:
        await query.answer(
            "Нет активного неоплаченного абонемента.", show_alert=True
        )
        return
    await query.answer()
    if query.message is None:
        return
    if len(subs) == 1:
        sub = subs[0]
        async with async_session_maker() as session:
            prompt = await _payin_amount_prompt(session, sub)
        await state.set_state(PayInFSM.amount)
        await state.update_data(person_id=person_id, subscription_id=sub.id)
        await query.message.answer(prompt)
        return
    async with async_session_maker() as session:
        rows: list[list[InlineKeyboardButton]] = []
        for sub in subs:
            product = await session.get(Product, sub.product_id)
            name = product.name if product is not None else "?"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=name,
                        callback_data=f"ppl_paypick:{sub.id}",
                    )
                ]
            )
    await state.update_data(person_id=person_id)
    await query.message.answer(
        "Выберите абонемент:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ppl_paypick:"))
async def cb_ppl_paypick(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    subscription_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    person_id = data.get("person_id")
    if not person_id:
        await query.answer("Ошибка", show_alert=True)
        return
    async with async_session_maker() as session:
        sub = await session.get(Subscription, subscription_id)
        if sub is None:
            await query.answer("Не найдено", show_alert=True)
            return
        prompt = await _payin_amount_prompt(session, sub)
    await query.answer()
    await state.set_state(PayInFSM.amount)
    await state.update_data(person_id=person_id, subscription_id=subscription_id)
    if query.message:
        await query.message.answer(prompt)


@router.message(PayInFSM.amount, F.text)
async def fsm_payin_amount(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    if message.text.strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Внесение оплаты отменено.")
        return
    try:
        amount = _parse_payment_amount(message.text)
    except InvalidOperation:
        await message.answer("Введите число.")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    data = await state.get_data()
    person_id = data.get("person_id")
    subscription_id = data.get("subscription_id")
    if not person_id or not subscription_id:
        await state.clear()
        await message.answer("Ошибка")
        return
    today = datetime.now(school_tz()).date()
    async with async_session_maker() as session:
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
            return
        await record_payment(
            session,
            str(subscription_id),
            amount,
            today,
            recorded_by_id=admin.id,
        )
        await session.commit()
        text = await _format_person_payments(session, str(person_id))
    await state.clear()
    await message.answer(text, reply_markup=_payments_keyboard(str(person_id)))


@router.callback_query(F.data.startswith("ppl_pair:"))
async def cb_ppl_pair(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    subscription_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        sub = await session.get(Subscription, subscription_id)
        if sub is None:
            await query.answer("Не найдено", show_alert=True)
            return
        membership = await _active_membership(session, sub.person_id)
        if membership is None:
            await query.answer("Нет активной группы", show_alert=True)
            return
        rows = await _list_group_client_pick_rows(
            session,
            membership.group_id,
            exclude_person_id=sub.person_id,
        )
    if not rows:
        await query.answer("Нет других клиентов в группе", show_alert=True)
        return
    await query.answer()
    if query.message:
        await _start_people_pick(
            query.message,
            state,
            purpose="ppl_pair",
            rows=rows,
            title="Номер выберет нового партнёра пары.",
        )
        await state.update_data(pair_subscription_id=subscription_id)


@router.callback_query(F.data.startswith("ppl_contact:"))
async def cb_ppl_contact(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None or person.role != "admin":
            await query.answer("Не найдено", show_alert=True)
            return
        card = await contact_for_person(session, person_id)
    await query.answer()
    if query.message is None:
        return
    if card is not None:
        await state.set_state(ContactEditFSM.name)
        await state.update_data(edit_contact_id=card.id)
        await query.message.answer('Имя. «то же» — не менять.')
        return
    await state.set_state(ContactFSM.name)
    await state.update_data(contact_person_id=person_id)
    await query.message.answer("Имя контакта:")


@router.callback_query(F.data.startswith("ppl_edit:"))
async def cb_ppl_edit(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None or person.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await state.set_state(PeoplePickFSM.card_name)
    await state.update_data(card_person_id=person_id)
    await query.answer()
    if query.message:
        await query.message.answer('Имя и фамилия. «то же» — не менять.')




@router.callback_query(F.data == "admin_invite")
async def cb_admin_invite(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(AdminInviteFSM.waiting_identifier)
    await query.answer()
    if query.message:
        await query.message.answer("Введите @username или телефон будущего админа:")


@router.callback_query(F.data == "admin_contact")
async def cb_admin_contact(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(ContactFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Имя контакта:")


@router.callback_query(F.data.startswith("adm_ask:"))
async def cb_adm_demote_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    target_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        target = await session.get(Person, target_id)
        if target is None or target.role != "admin":
            await query.answer("Не найдено", show_alert=True)
            return
        name = target.full_name
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать", callback_data=f"adm_yes:{target_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="adm_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(f"Снять права админа у {name}?", reply_markup=kb)


@router.callback_query(F.data == "adm_cancel")
async def cb_adm_demote_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Без изменений.")


@router.callback_query(F.data.startswith("adm_yes:"))
async def cb_adm_demote_yes(query: CallbackQuery) -> None:
    if query.data is None:
        return
    target_id = query.data.split(":", 1)[1]
    admin = await _require_admin_callback(query)
    if admin is None:
        return
    async with async_session_maker() as session:
        target = await session.get(Person, target_id)
        if target is None or target.role != "admin":
            await query.answer("Не найдено", show_alert=True)
            return
        name = target.full_name
        try:
            await demote_admin(session, admin.id, target_id)
            await session.commit()
        except ValueError as exc:
            await query.answer(_demote_admin_error(exc), show_alert=True)
            return
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report(f"{name} больше не админ", []))


@router.callback_query(F.data.startswith("ctc_card:"))
async def cb_ctc_card(query: CallbackQuery) -> None:
    if query.data is None:
        return
    contact_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        text = _contact_card_text(card)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить", callback_data=f"ctc_ren:{contact_id}"
                ),
                InlineKeyboardButton(
                    text="Убрать контакт", callback_data=f"ctc_ask:{contact_id}"
                ),
            ]
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ctc_ren:"))
async def cb_ctc_edit_start(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    contact_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
    await state.set_state(ContactEditFSM.name)
    await state.update_data(edit_contact_id=contact_id)
    await query.answer()
    if query.message:
        await query.message.answer('Имя. «то же» — не менять.')


@router.callback_query(F.data.startswith("ctc_ask:"))
async def cb_ctc_archive_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    contact_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await query.answer("Не найдено", show_alert=True)
            return
        name = card.name
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать", callback_data=f"ctc_yes:{contact_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="ctc_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(f"Убрать контакт «{name}»?", reply_markup=kb)


@router.callback_query(F.data == "ctc_cancel")
async def cb_ctc_archive_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Без изменений.")


@router.callback_query(F.data.startswith("ctc_yes:"))
async def cb_ctc_archive_yes(query: CallbackQuery) -> None:
    if query.data is None:
        return
    contact_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    now = datetime.now(UTC)
    async with async_session_maker() as session:
        try:
            await archive_contact(session, contact_id, now)
            await session.commit()
        except ValueError:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Контакт убран", []))


@router.callback_query(F.data == "ctc_arch")
async def cb_ctc_arch_list(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        cards = await list_contacts(session, archived=True)
    if not cards:
        await query.answer("Архив пуст", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Вернуть: {card.name}",
                    callback_data=f"ctc_rst:{card.id}",
                )
            ]
            for card in cards
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer("Архив контактов:", reply_markup=kb)


@router.callback_query(F.data.startswith("ctc_rst:"))
async def cb_ctc_restore(query: CallbackQuery) -> None:
    if query.data is None:
        return
    contact_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        try:
            await restore_contact(session, contact_id)
            await session.commit()
        except ValueError:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Контакт возвращён", []))
@router.message(F.text == BTN_PEOPLE)
async def menu_people(message: Message) -> None:
    async with async_session_maker() as session:
        if await _require_admin_message(message, session) is None:
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Список", callback_data="ppl_list")],
            [InlineKeyboardButton(text="Добавить", callback_data="ppl_add")],
            [InlineKeyboardButton(text="Архив", callback_data="ppl_arch")],
        ]
    )
    await message.answer("Пользователи — выберите действие:", reply_markup=kb)
@router.callback_query(F.data == "ppl_admins")
async def cb_ppl_admins(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        kb = await _admins_menu_keyboard(session)
    await query.answer()
    if query.message:
        await query.message.answer("Админы и контакты:", reply_markup=kb)
@router.message(F.text == BTN_CONTACTS)
async def menu_contacts(message: Message) -> None:
    async with async_session_maker() as session:
        await message.answer(await _format_contacts(session))
@router.message(PeopleAddFSM.name, F.text)
async def fsm_people_add_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            return
    items = parse_roster_line(message.text)
    item = items[0] if items else None
    if item is None or item.needs_fix or item.name_key is None:
        await message.answer("Фамилия и имя одной строкой.")
        return
    from .groups import _list_active_groups

    async with async_session_maker() as session:
        groups = await _list_active_groups(session)
    if not groups:
        await state.clear()
        await message.answer("Нет групп")
        return
    await state.update_data(
        add_full_name=item.full_name,
        add_name_key=item.name_key,
    )
    rows = [PickRow(id=g.id, full_name=g.name) for g in groups]
    await _start_list_pick(
        message,
        state,
        purpose="ppl_add",
        rows=rows,
        title="Номер выберет группу.",
        hint=HINT_INDEX,
    )


@router.message(PeoplePickFSM.waiting, F.text)
async def fsm_people_pick_waiting(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            return
    if message.text.strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Выбор отменён.")
        return
    data = await state.get_data()
    pick_ids: list[str] = list(data.get("pick_ids") or [])
    purpose = str(data.get("pick_purpose") or "")
    with_group_detail = _people_pick_shows_group(purpose)
    async with async_session_maker() as session:
        rows = await _pick_rows_for_ids(
            session,
            pick_ids,
            with_group_detail=with_group_detail,
            unpaid_only=purpose == "view",
        )
    try:
        picked = resolve_picks(rows, message.text, many=False)[0]
    except PickError as exc:
        if exc.message == _HOMONYM_PICK_ERROR:
            key = message.text.strip().casefold()
            homonym_ids = [
                row.id for row in rows if row.full_name.casefold() == key
            ]
            if len(homonym_ids) > 1:
                async with async_session_maker() as session:
                    homonym_rows = await _pick_rows_for_ids(
                        session,
                        homonym_ids,
                        with_group_detail=with_group_detail,
                        unpaid_only=purpose == "view",
                    )
                await state.update_data(pick_ids=homonym_ids, pick_page=0)
                await message.answer(exc.message)
                await _send_people_pick_page(message, homonym_rows, 0)
                return
        await message.answer(exc.message)
        return
    if purpose == "ppl_pair":
        sub_id = data.get("pair_subscription_id")
        if not sub_id:
            await state.clear()
            await message.answer("Ошибка")
            return
        async with async_session_maker() as session:
            try:
                await replace_partner(session, str(sub_id), picked.id)
                await session.commit()
            except ValueError as exc:
                await message.answer(_pair_error_message(exc))
                return
        await state.clear()
        partner_name = picked.full_name
        await message.answer(short_report(f"Партнёр: {partner_name}", []))
        return
    from .groups import _after_people_pick

    await _after_people_pick(message, state, picked.id, purpose)


@router.message(PeoplePickFSM.card_name, F.text)
async def fsm_people_card_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            return
    data = await state.get_data()
    person_id = data.get("card_person_id")
    if not person_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
    if person is None:
        await state.clear()
        await message.answer("Не найдено")
        return
    if message.text.strip().lower() == "то же":
        full_name = person.full_name
    else:
        full_name = message.text.strip()
    await state.update_data(card_full_name=full_name)
    await state.set_state(PeoplePickFSM.card_phone)
    await message.answer('Телефон. «то же» — не менять.')


@router.message(PeoplePickFSM.card_phone, F.text)
async def fsm_people_card_phone(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            return
    data = await state.get_data()
    person_id = data.get("card_person_id")
    full_name = data.get("card_full_name")
    if not person_id or not full_name:
        await state.clear()
        return
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        if person is None:
            await state.clear()
            await message.answer("Не найдено")
            return
        if message.text.strip().lower() == "то же":
            phone = person.phone
        else:
            raw = message.text.strip()
            phone = raw if raw else None
        try:
            updated = await update_person_card(
                session, person_id, str(full_name), phone
            )
            await session.commit()
        except ValueError:
            await message.answer("Не удалось обновить карточку.")
            return
    await state.clear()
    await message.answer(short_report("Карточка обновлена", [updated.full_name]))


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
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
            await state.clear()
            return
        invite = await invite_admin(session, admin, username=username, phone=phone)
        await session.commit()
        status = "активен" if invite.status == "active" else "ожидает входа в бота"
        await message.answer(f"Приглашение создано ({status}).")
    await state.clear()


@router.message(ContactFSM.name)
async def fsm_contact_name(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    await state.update_data(contact_name=message.text.strip())
    await state.set_state(ContactFSM.phone)
    await message.answer("Телефон:")


@router.message(ContactFSM.phone)
async def fsm_contact_phone(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    await state.update_data(contact_phone=message.text.strip())
    await state.set_state(ContactFSM.role_label)
    await message.answer("Роль (подпись):")


@router.message(ContactEditFSM.name, F.text)
async def fsm_contact_edit_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    contact_id = data.get("edit_contact_id")
    if not contact_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await state.clear()
            await message.answer("Не найдено")
            return
        name = card.name if message.text.strip().lower() == "то же" else message.text.strip()
    await state.update_data(edit_contact_name=name)
    await state.set_state(ContactEditFSM.phone)
    await message.answer('Телефон. «то же» — не менять.')


@router.message(ContactEditFSM.phone, F.text)
async def fsm_contact_edit_phone(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    contact_id = data.get("edit_contact_id")
    edit_name = data.get("edit_contact_name")
    if not contact_id or not edit_name:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await state.clear()
            await message.answer("Не найдено")
            return
        phone = card.phone if message.text.strip().lower() == "то же" else message.text.strip()
    await state.update_data(edit_contact_phone=phone)
    await state.set_state(ContactEditFSM.role_label)
    await message.answer('Роль (подпись). «то же» — не менять.')


@router.message(ContactEditFSM.role_label, F.text)
async def fsm_contact_edit_role(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    contact_id = data.get("edit_contact_id")
    edit_name = data.get("edit_contact_name")
    edit_phone = data.get("edit_contact_phone")
    if not contact_id or not edit_name or edit_phone is None:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        card = await session.get(ContactCard, contact_id)
        if card is None or card.archived_at is not None:
            await state.clear()
            await message.answer("Не найдено")
            return
        role_label = (
            card.role_label
            if message.text.strip().lower() == "то же"
            else message.text.strip()
        )
        try:
            await update_contact(session, contact_id, edit_name, edit_phone, role_label)
            await session.commit()
        except ValueError:
            await message.answer("Не удалось обновить контакт.")
            return
    await state.clear()
    await message.answer(short_report("Контакт обновлён", []))


@router.message(ContactFSM.role_label)
async def fsm_contact_role(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    person_id = data.get("contact_person_id")
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        try:
            await create_contact(
                session,
                data["contact_name"],
                data["contact_phone"],
                message.text.strip(),
                person_id=str(person_id) if person_id else None,
            )
            await session.commit()
        except ValueError:
            await message.answer("Не удалось сохранить контакт.")
            return
    await message.answer("Контакт сохранён.")
    await state.clear()


async def _build_rows_ppl_add(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    from .groups import _build_rows_active_groups

    return await _build_rows_active_groups(session, data)


register_pick_spec(
    "ppl_add",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_ppl_add,
        on_index_pick=_on_index_pick_ppl_add,
    ),
)
