"""Billing application services (products, prices, subscriptions)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, Price, Product, Reminder, Subscription

PaymentStatus = Literal["paid", "unpaid", "unknown"]

_VALID_AUDIENCES = frozenset({"person", "couple"})


def _validate_audience(audience: str) -> None:
    if audience not in _VALID_AUDIENCES:
        raise ValueError(f"audience must be one of: person, couple")


def subscription_covers_date(sub: Subscription, on_date: date) -> bool:
    if sub.starts_on is not None and on_date < sub.starts_on:
        return False
    if sub.valid_until is not None and on_date > sub.valid_until:
        return False
    return True


def _starts_on_clause(on_date: date):
    return (Subscription.starts_on.is_(None)) | (Subscription.starts_on <= on_date)


def _valid_until_clause(on_date: date):
    return (Subscription.valid_until.is_(None)) | (Subscription.valid_until >= on_date)


async def _has_active_couple_subscription(
    session: AsyncSession,
    person_id: str,
    on_date: date,
    exclude_subscription_id: str | None = None,
) -> bool:
    query = (
        select(Subscription.id)
        .where(
            Subscription.partner_id.isnot(None),
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
            Subscription.lessons_left > 0,
            _starts_on_clause(on_date),
            _valid_until_clause(on_date),
        )
        .limit(1)
    )
    if exclude_subscription_id is not None:
        query = query.where(Subscription.id != exclude_subscription_id)
    result = await session.execute(query)
    return result.scalar_one_or_none() is not None


async def create_product(
    session: AsyncSession,
    kind: str,
    name: str,
    lessons_count: int | None = None,
    validity_days: int | None = None,
    audience: str = "person",
) -> Product:
    _validate_audience(audience)
    if kind == "subscription":
        if lessons_count is None or validity_days is None:
            raise ValueError(
                "subscription requires lessons_count and validity_days"
            )
    elif kind == "drop_in":
        lessons_count = None
        validity_days = None
    else:
        raise ValueError(f"unknown product kind: {kind}")

    product = Product(
        kind=kind,
        name=name,
        lessons_count=lessons_count,
        validity_days=validity_days,
        audience=audience,
    )
    session.add(product)
    await session.flush()
    return product


async def set_price(
    session: AsyncSession,
    product_id: str,
    amount: Decimal,
    valid_from: date,
) -> Price:
    price = Price(product_id=product_id, amount=amount, valid_from=valid_from)
    session.add(price)
    await session.flush()
    return price


async def current_price(
    session: AsyncSession, product_id: str, on_date: date
) -> Price | None:
    result = await session.execute(
        select(Price)
        .where(
            Price.product_id == product_id,
            Price.valid_from <= on_date,
        )
        .order_by(Price.valid_from.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def subscription_payment_status(sub: Subscription) -> PaymentStatus:
    if sub.amount_due is None:
        return "unknown"
    if sub.amount_paid >= sub.amount_due:
        return "paid"
    return "unpaid"


async def record_payment(
    session: AsyncSession,
    subscription_id: str,
    amount: Decimal,
    paid_on: date,
    recorded_by_id: str | None = None,
) -> Payment:
    if amount <= 0:
        raise ValueError("payment amount must be positive")
    result = await session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    sub = result.scalar_one()
    payment = Payment(
        subscription_id=subscription_id,
        amount=amount,
        paid_on=paid_on,
        recorded_by_id=recorded_by_id,
    )
    session.add(payment)
    sub.amount_paid = sub.amount_paid + amount
    await session.flush()
    return payment


async def subscription_payments_count(
    session: AsyncSession, subscription_id: str
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Payment)
        .where(Payment.subscription_id == subscription_id)
    )
    return int(result.scalar_one())


async def delete_subscription(session: AsyncSession, subscription_id: str) -> None:
    """Remove a subscription that has no payments; otherwise refuse."""
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        raise ValueError("subscription not found")
    if sub.amount_paid > 0 or await subscription_payments_count(
        session, subscription_id
    ):
        raise ValueError("subscription has payments")
    await session.execute(
        update(Reminder)
        .where(Reminder.subscription_id == subscription_id)
        .values(subscription_id=None)
    )
    await session.delete(sub)
    await session.flush()


async def update_subscription(
    session: AsyncSession,
    subscription_id: str,
    *,
    starts_on: date | None,
    valid_until: date | None,
    lessons_left: int,
    amount_due: Decimal | None,
) -> Subscription:
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        raise ValueError("subscription not found")
    if lessons_left < 0:
        raise ValueError("lessons_left must not be negative")
    if starts_on is not None and valid_until is not None and valid_until < starts_on:
        raise ValueError("valid_until before starts_on")
    if amount_due is not None and amount_due < sub.amount_paid:
        raise ValueError("amount_due is less than already paid")
    sub.starts_on = starts_on
    sub.valid_until = valid_until
    sub.lessons_left = lessons_left
    sub.amount_due = amount_due
    await session.flush()
    return sub


async def payable_subscription(
    session: AsyncSession, person_id: str, on_date: date
) -> Subscription | None:
    """Active subscription that still has an unpaid balance."""
    sub = await active_subscription(session, person_id, on_date)
    if sub is None or subscription_payment_status(sub) != "unpaid":
        return None
    return sub


async def list_payable_subscriptions(
    session: AsyncSession, person_id: str, on_date: date
) -> list[Subscription]:
    result = await session.execute(
        select(Subscription)
        .where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
            Subscription.lessons_left > 0,
            _starts_on_clause(on_date),
            _valid_until_clause(on_date),
        )
        .order_by(Subscription.valid_until.asc().nullsfirst())
    )
    subs = list(result.scalars().all())
    return [s for s in subs if subscription_payment_status(s) == "unpaid"]


async def person_has_unpaid_started(
    session: AsyncSession, person_id: str, today: date
) -> bool:
    result = await session.execute(
        select(Subscription).where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
        )
    )
    for sub in result.scalars().all():
        if subscription_payment_status(sub) != "unpaid":
            continue
        if sub.starts_on is not None and sub.starts_on > today:
            continue
        return True
    return False


async def replace_partner(
    session: AsyncSession, subscription_id: str, new_partner_id: str
) -> Subscription:
    result = await session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    sub = result.scalar_one()
    product = await _get_product(session, sub.product_id)
    if product.audience != "couple":
        raise ValueError("subscription product is not for couples")
    if new_partner_id == sub.person_id:
        raise ValueError("partner must differ from the primary person")
    today = date.today()
    if await _has_active_couple_subscription(
        session,
        new_partner_id,
        today,
        exclude_subscription_id=subscription_id,
    ):
        raise ValueError("person already has an active couple subscription")
    sub.partner_id = new_partner_id
    await session.flush()
    return sub


async def open_subscription(
    session: AsyncSession,
    person_id: str,
    product_id: str,
    started_on: date,
    partner_id: str | None = None,
    *,
    starts_on: date | None = None,
    amount_due: Decimal | None = None,
    amount_paid: Decimal = Decimal("0"),
    recorded_by_id: str | None = None,
) -> Subscription:
    result = await session.execute(
        select(Product).where(Product.id == product_id)
    )
    product = result.scalar_one()
    if product.kind != "subscription":
        raise ValueError("product must be a subscription")
    if product.lessons_count is None or product.validity_days is None:
        raise ValueError("subscription product missing lessons_count or validity_days")

    if product.audience == "couple":
        if partner_id is None:
            raise ValueError("couple subscription requires partner_id")
        if partner_id == person_id:
            raise ValueError("partner must differ from the primary person")
        for pid in (person_id, partner_id):
            if await _has_active_couple_subscription(session, pid, started_on):
                raise ValueError(
                    "person already has an active couple subscription"
                )
    elif product.audience == "person":
        if partner_id is not None:
            raise ValueError("person subscription must not have a partner")
    else:
        raise ValueError(f"unknown product audience: {product.audience}")

    valid_until: date | None = None
    if starts_on is not None and product.validity_days is not None:
        valid_until = starts_on + timedelta(days=product.validity_days)

    paid = amount_paid
    if paid < 0:
        raise ValueError("amount_paid must not be negative")

    subscription = Subscription(
        person_id=person_id,
        partner_id=partner_id,
        product_id=product_id,
        lessons_left=product.lessons_count,
        valid_until=valid_until,
        starts_on=starts_on,
        amount_due=amount_due,
        amount_paid=paid,
    )
    session.add(subscription)
    await session.flush()

    if paid > 0:
        payment = Payment(
            subscription_id=subscription.id,
            amount=paid,
            paid_on=starts_on if starts_on is not None else started_on,
            recorded_by_id=recorded_by_id,
        )
        session.add(payment)
        await session.flush()

    return subscription


async def issue_subscription(
    session: AsyncSession,
    person_id: str,
    product_id: str,
    started_on: date,
    partner_id: str | None = None,
    *,
    starts_on: date | None = None,
    amount_due: Decimal | None = None,
    amount_paid: Decimal = Decimal("0"),
    recorded_by_id: str | None = None,
) -> Subscription:
    """Alias for open_subscription with billing fields."""
    return await open_subscription(
        session,
        person_id,
        product_id,
        started_on,
        partner_id,
        starts_on=starts_on,
        amount_due=amount_due,
        amount_paid=amount_paid,
        recorded_by_id=recorded_by_id,
    )


async def active_subscription(
    session: AsyncSession, person_id: str, on_date: date
) -> Subscription | None:
    result = await session.execute(
        select(Subscription)
        .where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
            Subscription.lessons_left > 0,
            _starts_on_clause(on_date),
            _valid_until_clause(on_date),
        )
        .order_by(Subscription.valid_until.asc().nullsfirst())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def covering_subscription_for_refund(
    session: AsyncSession, person_id: str, on_date: date
) -> Subscription | None:
    """Subscription that covered on_date, even if lessons_left is already 0."""
    result = await session.execute(
        select(Subscription)
        .where(
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
            _starts_on_clause(on_date),
            _valid_until_clause(on_date),
        )
        .order_by(Subscription.valid_until.asc().nullsfirst())
    )
    for sub in result.scalars().all():
        if subscription_covers_date(sub, on_date):
            return sub
    return None


async def _get_product(session: AsyncSession, product_id: str) -> Product:
    result = await session.execute(
        select(Product).where(Product.id == product_id)
    )
    return result.scalar_one()


async def update_product(
    session: AsyncSession,
    product_id: str,
    name: str,
    lessons_count: int | None = None,
    validity_days: int | None = None,
    audience: str | None = None,
) -> Product:
    product = await _get_product(session, product_id)
    stripped = name.strip()
    if not stripped:
        raise ValueError("name must not be empty")
    product.name = stripped
    if product.kind == "subscription":
        if lessons_count is None or validity_days is None:
            raise ValueError(
                "subscription requires lessons_count and validity_days"
            )
        if lessons_count <= 0 or validity_days <= 0:
            raise ValueError(
                "subscription requires positive lessons_count and validity_days"
            )
        product.lessons_count = lessons_count
        product.validity_days = validity_days
    elif product.kind == "drop_in":
        product.lessons_count = None
        product.validity_days = None
    if audience is not None:
        _validate_audience(audience)
        product.audience = audience
    await session.flush()
    return product


async def deactivate_product(session: AsyncSession, product_id: str) -> Product:
    product = await _get_product(session, product_id)
    product.active = False
    await session.flush()
    return product


async def restore_product(session: AsyncSession, product_id: str) -> Product:
    product = await _get_product(session, product_id)
    product.active = True
    await session.flush()
    return product


async def list_active_products_with_prices(
    session: AsyncSession, on_date: date
) -> list[tuple[Product, Price]]:
    result = await session.execute(
        select(Product).where(Product.active.is_(True)).order_by(Product.name)
    )
    products = result.scalars().all()
    out: list[tuple[Product, Price]] = []
    for product in products:
        price = await current_price(session, product.id, on_date)
        if price is not None:
            out.append((product, price))
    return out
