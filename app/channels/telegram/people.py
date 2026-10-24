"""Telegram people domain: clients, admins, contacts."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import ContactCard, Group, GroupMembership, Person
from app.services.contacts import (
    archive_contact,
    list_contacts,
    restore_contact,
    update_contact,
)
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
from app.services.schedule import active_group_membership, assign_group

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
    PeopleAddFSM,
    PeoplePickFSM,
)

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


async def _person_group_detail(session: AsyncSession, person_id: str) -> str:
    membership = await _active_membership(session, person_id)
    if membership is None:
        return "без группы"
    group = await session.get(Group, membership.group_id)
    return group.name if group is not None else "без группы"


async def _pick_rows_for_ids(
    session: AsyncSession, pick_ids: list[str], *, with_group_detail: bool
) -> list[PickRow]:
    if not pick_ids:
        return []
    result = await session.execute(select(Person).where(Person.id.in_(pick_ids)))
    by_id = {p.id: p for p in result.scalars().all()}
    rows: list[PickRow] = []
    for person_id in pick_ids:
        person = by_id.get(person_id)
        if person is None:
            continue
        detail = await _person_group_detail(session, person_id) if with_group_detail else ""
        rows.append(PickRow(id=person.id, full_name=person.full_name, detail=detail))
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


def _person_card_keyboard(person_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
                    text="Убрать клиента",
                    callback_data=f"ppl_arch_ask:{person_id}",
                )
            ],
        ]
    )


async def _person_card_text(session: AsyncSession, person_id: str) -> str | None:
    person = await session.get(Person, person_id)
    if person is None or person.archived_at is not None:
        return None
    lines = [person.full_name]
    if person.phone:
        lines.append(f"Телефон: {person.phone}")
    detail = await _person_group_detail(session, person_id)
    lines.append(f"Группа: {detail}")
    return "\n".join(lines)


async def _send_person_card(message: Message, person_id: str) -> None:
    async with async_session_maker() as session:
        text = await _person_card_text(session, person_id)
    if text is None:
        await message.answer("Не найдено")
        return
    await message.answer(text, reply_markup=_person_card_keyboard(person_id))


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


def _people_pick_keyboard(rows: list[PickRow], page: int) -> InlineKeyboardMarkup | None:
    if not has_next_page(rows, page):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Дальше", callback_data="pp_more")]
        ]
    )


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
@router.callback_query(F.data == "pp_more")
async def cb_people_pick_more(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    data = await state.get_data()
    pick_ids: list[str] = list(data.get("pick_ids") or [])
    page = int(data.get("pick_page") or 0) + 1
    purpose = str(data.get("pick_purpose") or "")
    with_group_detail = purpose in ("assign", "card", "leave", "view")
    async with async_session_maker() as session:
        rows = await _pick_rows_for_ids(
            session, pick_ids, with_group_detail=with_group_detail
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
        rows = await _list_active_client_pick_rows(session)
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
            title="Номер откроет карточку клиента.",
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
        from .legacy import _prompt_assign_group

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
            [InlineKeyboardButton(text="Список клиентов", callback_data="ppl_list")],
            [InlineKeyboardButton(text="Добавить клиента", callback_data="ppl_add")],
            [InlineKeyboardButton(text="Архив клиентов", callback_data="ppl_arch")],
            [InlineKeyboardButton(text="Гости", callback_data="ppl_guests")],
            [InlineKeyboardButton(text="Админы", callback_data="ppl_admins")],
        ]
    )
    await message.answer("Люди — выберите действие:", reply_markup=kb)
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
    from .legacy import _list_active_groups

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
    data = await state.get_data()
    pick_ids: list[str] = list(data.get("pick_ids") or [])
    purpose = str(data.get("pick_purpose") or "")
    with_group_detail = purpose in ("assign", "card", "leave", "view")
    async with async_session_maker() as session:
        rows = await _pick_rows_for_ids(
            session, pick_ids, with_group_detail=with_group_detail
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
                    )
                await state.update_data(pick_ids=homonym_ids, pick_page=0)
                await message.answer(exc.message)
                await _send_people_pick_page(message, homonym_rows, 0)
                return
        await message.answer(exc.message)
        return
    from .legacy import _after_people_pick

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
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
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


async def _build_rows_ppl_add(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    from .legacy import _build_rows_active_groups

    return await _build_rows_active_groups(session, data)


register_pick_spec(
    "ppl_add",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_ppl_add,
        on_index_pick=_on_index_pick_ppl_add,
    ),
)
