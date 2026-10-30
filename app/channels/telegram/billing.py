"""Telegram billing domain: price list, products, subscriptions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

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
from app.models import Person, Product
from app.services.billing import (
    create_product,
    current_price,
    deactivate_product,
    list_active_products_with_prices,
    open_subscription,
    restore_product,
    set_price,
    update_product,
)
from app.services.pick_list import HINT_INDEX, PickRow, page_text, short_report
from app.services.schedule import parse_dmy

from .common import (
    purge_button_row,
    BTN_PRICE,
    PickSpec,
    _BlockArchivedMiddleware,
    _TrackUserMessageMiddleware,
    _is_admin,
    _list_pick_keyboard,
    _person_by_telegram,
    _pick_ids_from_data,
    _require_admin_callback,
    _require_admin_message_strict,
    _start_list_pick,
    _today_local,
    register_pick_spec,
)
from .states import (
    DropInProductFSM,
    ListPickFSM,
    NewPriceFSM,
    OpenSubFSM,
    ProductEditFSM,
    SubProductFSM,
)

router = Router(name="telegram.billing")

router.message.middleware(_TrackUserMessageMiddleware())
router.message.middleware(_BlockArchivedMiddleware())
router.callback_query.middleware(_BlockArchivedMiddleware())

_OPEN_SUB_CANCEL = "отмена"
_PROMPT_START_DATE = (
    "Дата начала абонемента (ДД.ММ.ГГГГ), можно в прошлом:"
)
_PROMPT_PAYMENT = "Оплата: да / частично / нет"
_PROMPT_PARTIAL_AMOUNT = "Сумма оплаты (число, запятая или точка):"


def parse_open_sub_start_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d.%m.%Y").date()


def parse_open_sub_payment_choice(text: str) -> str | None:
    lowered = text.strip().lower()
    if lowered == "да":
        return "yes"
    if lowered == "нет":
        return "no"
    if lowered == "частично":
        return "partial"
    return None


def parse_open_sub_amount(text: str) -> Decimal:
    return Decimal(text.strip().replace(",", "."))


def partial_amount_error(amount: Decimal, amount_due: Decimal | None) -> str | None:
    if amount <= 0:
        return "Сумма должна быть больше нуля."
    if amount_due is not None and amount >= amount_due:
        return "Сумма должна быть меньше полной цены."
    return None


def amount_paid_for_open_sub(
    choice: str, amount_due: Decimal | None, partial: Decimal | None = None
) -> Decimal:
    if choice == "no":
        return Decimal("0")
    if choice == "yes":
        return amount_due if amount_due is not None else Decimal("0")
    if partial is None:
        raise ValueError("partial amount required")
    return partial


def _amount_due_from_fsm_data(data: dict[str, object]) -> Decimal | None:
    raw = data.get("amount_due")
    if raw is None:
        return None
    return Decimal(str(raw))


async def _clear_open_sub_cancel(message: Message, state: FSMContext) -> bool:
    if message.text and message.text.strip().lower() == _OPEN_SUB_CANCEL:
        await state.clear()
        await message.answer("Выдача абонемента отменена.")
        return True
    return False


async def _build_rows_prd(
    session: AsyncSession, data: dict[str, object]
) -> list[PickRow]:
    return await _product_pick_rows(session, _pick_ids_from_data(data))


async def _on_index_pick_prd(
    message: Message, state: FSMContext, picked: list[PickRow]
) -> None:
    await _send_product_card(message, picked[0].id)

async def _prompt_open_sub_products(
    message: Message, state: FSMContext, person_id: str
) -> None:
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
        await state.clear()
        await message.answer("Нет абонементов")
        return
    await state.set_state(OpenSubFSM.product_id)
    await state.update_data(person_id=person_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p.name, callback_data=f"open_sub_prod:{p.id}")]
            for p in products
        ]
    )
    await message.answer("Выберите абонемент:", reply_markup=kb)


async def _report_open_subscription(
    message: Message,
    state: FSMContext,
    *,
    person_id: str,
    product_id: str,
    partner_id: str | None = None,
) -> None:
    await state.update_data(
        person_id=person_id,
        product_id=product_id,
        partner_id=partner_id,
    )
    await state.set_state(OpenSubFSM.starts_on)
    await message.answer(_PROMPT_START_DATE)


async def _finalize_open_subscription(
    message: Message,
    state: FSMContext,
    *,
    person_id: str,
    product_id: str,
    partner_id: str | None,
    starts_on: date,
    amount_due: Decimal | None,
    amount_paid: Decimal,
    recorded_by_id: str,
) -> None:
    async with async_session_maker() as session:
        person = await session.get(Person, person_id)
        product = await session.get(Product, product_id)
        partner = (
            await session.get(Person, partner_id) if partner_id is not None else None
        )
        try:
            await open_subscription(
                session,
                person_id,
                product_id,
                starts_on,
                partner_id=partner_id,
                starts_on=starts_on,
                amount_due=amount_due,
                amount_paid=amount_paid,
                recorded_by_id=recorded_by_id,
            )
            await session.commit()
        except ValueError as exc:
            await message.answer(str(exc))
            return
    await state.clear()
    names: list[str] = []
    if person is not None:
        names.append(person.full_name)
    if partner is not None:
        names.append(partner.full_name)
    title = (
        f"Абонемент «{product.name}» выдан"
        if product is not None
        else "Абонемент выдан"
    )
    await message.answer(short_report(title, names))


@router.message(OpenSubFSM.starts_on)
async def fsm_open_sub_starts_on(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    if await _clear_open_sub_cancel(message, state):
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    try:
        starts_on = parse_open_sub_start_date(message.text)
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ")
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        price = await current_price(session, str(product_id), starts_on)
    amount_due = price.amount if price is not None else None
    await state.update_data(
        starts_on=starts_on.isoformat(),
        amount_due=str(amount_due) if amount_due is not None else None,
    )
    await state.set_state(OpenSubFSM.payment)
    await message.answer(_PROMPT_PAYMENT)


@router.message(OpenSubFSM.payment)
async def fsm_open_sub_payment(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    if await _clear_open_sub_cancel(message, state):
        return
    choice = parse_open_sub_payment_choice(message.text)
    if choice is None:
        await message.answer(_PROMPT_PAYMENT)
        return
    data = await state.get_data()
    amount_due = _amount_due_from_fsm_data(data)
    if choice == "partial":
        await state.update_data(payment_choice=choice)
        await state.set_state(OpenSubFSM.partial_amount)
        await message.answer(_PROMPT_PARTIAL_AMOUNT)
        return
    async with async_session_maker() as session:
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
            await state.clear()
            return
    starts_on = date.fromisoformat(str(data["starts_on"]))
    person_id = str(data["person_id"])
    product_id = str(data["product_id"])
    partner_raw = data.get("partner_id")
    partner_id = str(partner_raw) if partner_raw is not None else None
    amount_paid = amount_paid_for_open_sub(choice, amount_due)
    await _finalize_open_subscription(
        message,
        state,
        person_id=person_id,
        product_id=product_id,
        partner_id=partner_id,
        starts_on=starts_on,
        amount_due=amount_due,
        amount_paid=amount_paid,
        recorded_by_id=admin.id,
    )


@router.message(OpenSubFSM.partial_amount)
async def fsm_open_sub_partial_amount(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    if await _clear_open_sub_cancel(message, state):
        return
    try:
        amount = parse_open_sub_amount(message.text)
    except InvalidOperation:
        await message.answer("Некорректная сумма.")
        return
    data = await state.get_data()
    amount_due = _amount_due_from_fsm_data(data)
    err = partial_amount_error(amount, amount_due)
    if err is not None:
        await message.answer(err)
        return
    async with async_session_maker() as session:
        admin = await _require_admin_message_strict(message, session)
        if admin is None:
            await state.clear()
            return
    starts_on = date.fromisoformat(str(data["starts_on"]))
    person_id = str(data["person_id"])
    product_id = str(data["product_id"])
    partner_raw = data.get("partner_id")
    partner_id = str(partner_raw) if partner_raw is not None else None
    amount_paid = amount_paid_for_open_sub("partial", amount_due, amount)
    await _finalize_open_subscription(
        message,
        state,
        person_id=person_id,
        product_id=product_id,
        partner_id=partner_id,
        starts_on=starts_on,
        amount_due=amount_due,
        amount_paid=amount_paid,
        recorded_by_id=admin.id,
    )

@router.callback_query(F.data.startswith("open_sub_p:"))
async def cb_open_sub_person(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    person_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await query.answer()
    if query.message:
        await _prompt_open_sub_products(query.message, state, person_id)


@router.callback_query(F.data.startswith("open_sub_prod:"))
async def cb_open_sub_product(query: CallbackQuery, state: FSMContext) -> None:
    from .people import _list_client_pick_rows

    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    data = await state.get_data()
    person_id = data.get("person_id")
    if not person_id:
        await query.answer("Ошибка")
        return
    if await _require_admin_callback(query) is None:
        return
    await query.answer()
    if query.message is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        audience = getattr(product, "audience", None) or "person"
        if audience == "couple":
            rows = [
                row
                for row in await _list_client_pick_rows(session)
                if row.id != person_id
            ]
            if not rows:
                await query.message.answer("Нет других клиентов")
                await state.clear()
                return
            await state.update_data(product_id=product_id)
            await _start_list_pick(
                query.message,
                state,
                purpose="sub_partner",
                rows=rows,
                title="Номер выберет второго человека пары.",
                hint=HINT_INDEX,
            )
            return
    await _report_open_subscription(
        query.message,
        state,
        person_id=str(person_id),
        product_id=product_id,
    )


@router.callback_query(F.data.startswith("new_price_p:"))
async def cb_new_price_product(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(NewPriceFSM.amount)
    await state.update_data(product_id=product_id)
    await query.answer()
    if query.message:
        await query.message.answer("Новая сумма (например 120.00):")

@router.callback_query(F.data == "price_sub")
async def cb_price_sub(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(SubProductFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название абонемента:")


@router.callback_query(F.data == "price_dropin")
async def cb_price_dropin(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
        return
    await state.set_state(DropInProductFSM.name)
    await query.answer()
    if query.message:
        await query.message.answer("Название разового занятия:")


@router.callback_query(F.data == "price_open_sub")
async def cb_price_open_sub(query: CallbackQuery, state: FSMContext) -> None:
    from .people import _list_client_pick_rows, _start_people_pick

    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        rows = await _list_client_pick_rows(session)
    if not rows:
        await query.answer("Нет клиентов", show_alert=True)
        return
    await query.answer()
    if query.message:
        await _start_people_pick(
            query.message,
            state,
            purpose="subscription",
            rows=rows,
            title="Выдача абонемента по номеру.",
        )


@router.callback_query(F.data == "price_new_price")
async def cb_price_new_price(query: CallbackQuery, state: FSMContext) -> None:
    if await _require_admin_callback(query) is None:
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


def _product_kind_label(kind: str) -> str:
    return "абонемент" if kind == "subscription" else "разовое"


def _product_audience_label(audience: str | None) -> str:
    if audience == "couple":
        return "семейная пара"
    return "1 человек"


def _subscription_audience_keyboard(
    prefix: str, *, include_same: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="1 человек", callback_data=f"{prefix}:person")],
        [
            InlineKeyboardButton(
                text="семейная пара", callback_data=f"{prefix}:couple"
            )
        ],
    ]
    if include_same:
        rows.append(
            [InlineKeyboardButton(text="то же", callback_data=f"{prefix}:same")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _product_card_text(session: AsyncSession, product: Product, on_date: date) -> str:
    kind = _product_kind_label(product.kind)
    lines = [product.name, f"Тип: {kind}"]
    if product.kind == "subscription":
        audience = getattr(product, "audience", None) or "person"
        lines.append(f"Для кого: {_product_audience_label(audience)}")
    if product.kind == "subscription" and product.lessons_count is not None:
        lines.append(
            f"Занятий: {product.lessons_count}, срок: {product.validity_days} дн."
        )
    price = await current_price(session, product.id, on_date)
    if price is not None:
        lines.append(f"Текущая цена: {price.amount}")
    return "\n".join(lines)


def _product_card_keyboard(product_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить", callback_data=f"prd_ren:{product_id}"
                ),
                InlineKeyboardButton(
                    text="Убрать из прайса", callback_data=f"prd_ask:{product_id}"
                ),
            ]
        ]
    )


async def _send_product_card(message: Message, product_id: str) -> None:
    today = _today_local()
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await message.answer("Не найдено")
            return
        text = await _product_card_text(session, product, today)
    await message.answer(text, reply_markup=_product_card_keyboard(product_id))


@router.callback_query(F.data.startswith("prd_card:"))
async def cb_prd_card(query: CallbackQuery) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await query.answer("Не найдено", show_alert=True)
            return
    await query.answer()
    if query.message:
        await _send_product_card(query.message, product_id)


@router.callback_query(F.data.startswith("prd_ren:"))
async def cb_prd_rename(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await query.answer("Не найдено", show_alert=True)
            return
        kind = product.kind
        lessons = product.lessons_count
        validity = product.validity_days
        name = product.name
    await state.set_state(ProductEditFSM.name)
    await state.update_data(
        product_id=product_id,
        product_kind=kind,
        product_name=name,
        product_lessons=lessons,
        product_validity=validity,
    )
    await query.answer()
    if query.message:
        await query.message.answer(
            "Название. Чтобы оставить прежнее, отправьте «то же»."
        )


@router.message(ProductEditFSM.name)
async def fsm_prd_edit_name(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    name = (
        data["product_name"]
        if message.text.strip().lower() == "то же"
        else message.text.strip()
    )
    await state.update_data(edit_name=name)
    if data.get("product_kind") == "subscription":
        await state.set_state(ProductEditFSM.audience)
        await message.answer(
            "Для кого?",
            reply_markup=_subscription_audience_keyboard("prd_aud", include_same=True),
        )
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        try:
            product = await update_product(session, product_id, name)
            await session.commit()
        except ValueError:
            await message.answer("Некорректное название.")
            return
        final_name = product.name
    await state.clear()
    await message.answer(short_report(f"Продукт теперь «{final_name}»", []))


@router.message(ProductEditFSM.lessons)
async def fsm_prd_edit_lessons(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    if not data.get("product_id"):
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    if message.text.strip().lower() == "то же":
        lessons = data["product_lessons"]
    else:
        try:
            lessons = int(message.text.strip())
        except ValueError:
            await message.answer("Введите целое число")
            return
    await state.update_data(edit_lessons=lessons)
    await state.set_state(ProductEditFSM.validity)
    await message.answer("Срок (дней). Чтобы оставить прежнее, отправьте «то же».")


@router.message(ProductEditFSM.validity)
async def fsm_prd_edit_validity(message: Message, state: FSMContext) -> None:
    if not message.text or message.from_user is None:
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        return
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
    if message.text.strip().lower() == "то же":
        validity = data["product_validity"]
    else:
        try:
            validity = int(message.text.strip())
        except ValueError:
            await message.answer("Введите целое число")
            return
    name = data["edit_name"]
    lessons = data["edit_lessons"]
    async with async_session_maker() as session:
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        try:
            product = await update_product(
                session,
                product_id,
                name,
                lessons_count=lessons,
                validity_days=validity,
                audience=data.get("edit_audience"),
            )
            await session.commit()
        except ValueError:
            await message.answer("Некорректные данные.")
            return
        final_name = product.name
    await state.clear()
    await message.answer(short_report(f"Продукт теперь «{final_name}»", []))


@router.callback_query(F.data.startswith("prd_aud:"))
async def cb_prd_edit_audience(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    choice = query.data.split(":", 1)[1]
    if choice not in ("person", "couple", "same"):
        return
    if await _require_admin_callback(query) is None:
        return
    edit_audience = None if choice == "same" else choice
    await state.update_data(edit_audience=edit_audience)
    await state.set_state(ProductEditFSM.lessons)
    await query.answer()
    if query.message:
        await query.message.answer(
            "Число занятий. Чтобы оставить прежнее, отправьте «то же»."
        )


@router.callback_query(F.data.startswith("prd_ask:"))
async def cb_prd_deactivate_ask(query: CallbackQuery) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await query.answer("Не найдено", show_alert=True)
            return
        name = product.name
    text = (
        f"Убрать «{name}» из прайса?\n"
        "Уже выданные абонементы действуют до конца срока."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать", callback_data=f"prd_yes:{product_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="prd_cancel")],
        ]
    )
    await query.answer()
    if query.message:
        await query.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "prd_cancel")
async def cb_prd_cancel(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    await query.answer("Отменено")
    if query.message:
        await query.message.answer("Без изменений.")


@router.callback_query(F.data.startswith("prd_yes:"))
async def cb_prd_deactivate_yes(query: CallbackQuery) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await query.answer("Не найдено", show_alert=True)
            return
        await deactivate_product(session, product_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Продукт скрыт из прайса", []))


@router.callback_query(F.data == "prd_arch")
async def cb_prd_arch_list(query: CallbackQuery) -> None:
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        products = list(
            (
                await session.execute(
                    select(Product)
                    .where(Product.active.is_(False))
                    .order_by(Product.name)
                )
            ).scalars().all()
        )
    if not products:
        await query.answer("Архив пуст", show_alert=True)
        return
    kb_rows: list[list[InlineKeyboardButton]] = []
    for p in products:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"Вернуть: {p.name}",
                    callback_data=f"prd_back:{p.id}",
                )
            ]
        )
        kb_rows.append(purge_button_row("product", p.id, p.name))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await query.answer()
    if query.message:
        await query.message.answer("Архив продуктов:", reply_markup=kb)


@router.callback_query(F.data.startswith("prd_back:"))
async def cb_prd_restore(query: CallbackQuery) -> None:
    if query.data is None:
        return
    product_id = query.data.split(":", 1)[1]
    if await _require_admin_callback(query) is None:
        return
    async with async_session_maker() as session:
        product = await session.get(Product, product_id)
        if product is None or product.active:
            await query.answer("Не найдено", show_alert=True)
            return
        await restore_product(session, product_id)
        await session.commit()
    await query.answer("Готово")
    if query.message:
        await query.message.answer(short_report("Продукт возвращён", []))

async def _product_pick_rows(
    session: AsyncSession, product_ids: list[str] | None = None
) -> list[PickRow]:
    today = _today_local()
    if product_ids is None:
        products = list(
            (
                await session.execute(
                    select(Product)
                    .where(Product.active.is_(True))
                    .order_by(Product.name)
                )
            ).scalars().all()
        )
    else:
        products = []
        for product_id in product_ids:
            product = await session.get(Product, product_id)
            if product is not None and product.active:
                products.append(product)
    rows: list[PickRow] = []
    for product in products:
        price = await current_price(session, product.id, today)
        detail = str(price.amount) if price is not None else "нет цены"
        rows.append(PickRow(id=product.id, full_name=product.name, detail=detail))
    return rows


@router.message(F.text == BTN_PRICE)
async def menu_price(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        person = await _person_by_telegram(session, message.from_user.id)  # type: ignore[union-attr]
        if person is None:
            return
        if not _is_admin(person):
            today = _today_local()
            items = await list_active_products_with_prices(session, today)
            lines = ["Прайс:"]
            for product, price in items:
                kind = "абонемент" if product.kind == "subscription" else "разовое"
                extra = ""
                if product.lessons_count:
                    extra = f", {product.lessons_count} зан., {product.validity_days} дн."
                lines.append(f"• {product.name} ({kind}{extra}): {price.amount}")
            await message.answer("\n".join(lines) if items else "Прайс пока пуст.")
            return
        prd_rows = await _product_pick_rows(session)
        kb_rows: list[list[InlineKeyboardButton]] = [
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
            [
                InlineKeyboardButton(
                    text="Архив продуктов", callback_data="prd_arch"
                )
            ],
        ]
        if prd_rows:
            await state.set_state(ListPickFSM.waiting)
            await state.update_data(
                pick_ids=[row.id for row in prd_rows],
                pick_page=0,
                pick_purpose="prd",
            )
            body = page_text(prd_rows, 0, HINT_INDEX)
            text = f"Номер откроет карточку продукта.\n\n{body}"
            more = _list_pick_keyboard(prd_rows, 0, "prd")
            if more is not None:
                kb_rows.extend(more.inline_keyboard)
        else:
            text = "Прайс пока пуст."
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@router.message(SubProductFSM.name)
async def fsm_sub_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(sub_name=message.text.strip())
    await state.set_state(SubProductFSM.audience)
    await message.answer(
        "Для кого?",
        reply_markup=_subscription_audience_keyboard("sub_aud"),
    )


@router.callback_query(F.data.startswith("sub_aud:"))
async def cb_sub_audience(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
    audience = query.data.split(":", 1)[1]
    if audience not in ("person", "couple"):
        return
    if await _require_admin_callback(query) is None:
        return
    await state.update_data(sub_audience=audience)
    await state.set_state(SubProductFSM.lessons)
    await query.answer()
    if query.message:
        await query.message.answer("Число занятий:")


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
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        product = await create_product(
            session,
            "subscription",
            data["sub_name"],
            lessons_count=int(data["sub_lessons"]),
            validity_days=int(data["sub_validity"]),
            audience=data.get("sub_audience", "person"),
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
        if await _require_admin_message_strict(message, session) is None:
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
        if await _require_admin_message_strict(message, session) is None:
            await state.clear()
            return
        await set_price(session, data["product_id"], amount, valid_from)
        await session.commit()
    await message.answer("Цена сохранена.")
    await state.clear()

register_pick_spec(
    "prd",
    PickSpec(
        is_index_pick=True,
        build_rows=_build_rows_prd,
        on_index_pick=_on_index_pick_prd,
    ),
)
