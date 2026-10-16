"""Billing application services (products, prices, subscriptions)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Price, Product, Subscription

_VALID_AUDIENCES = frozenset({"person", "couple"})


def _validate_audience(audience: str) -> None:
    if audience not in _VALID_AUDIENCES:
        raise ValueError(f"audience must be one of: person, couple")


async def _has_active_couple_subscription(
    session: AsyncSession, person_id: str, on_date: date
) -> bool:
    result = await session.execute(
        select(Subscription.id)
        .where(
            Subscription.partner_id.isnot(None),
            or_(
                Subscription.person_id == person_id,
                Subscription.partner_id == person_id,
            ),
            Subscription.lessons_left > 0,
            (Subscription.valid_until.is_(None))
            | (Subscription.valid_until >= on_date),
        )
        .limit(1)
    )
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


async def open_subscription(
    session: AsyncSession,
    person_id: str,
    product_id: str,
    started_on: date,
    partner_id: str | None = None,
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

    subscription = Subscription(
        person_id=person_id,
        partner_id=partner_id,
        product_id=product_id,
        lessons_left=product.lessons_count,
        valid_until=None,
    )
    session.add(subscription)
    await session.flush()
    return subscription


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
            (Subscription.valid_until.is_(None))
            | (Subscription.valid_until >= on_date),
        )
        .order_by(Subscription.valid_until.asc().nullsfirst())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
