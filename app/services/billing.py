"""Billing application services (products, prices, subscriptions)."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Price, Product, Subscription


async def create_product(
    session: AsyncSession,
    kind: str,
    name: str,
    lessons_count: int | None = None,
    validity_days: int | None = None,
) -> Product:
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
) -> Subscription:
    result = await session.execute(
        select(Product).where(Product.id == product_id)
    )
    product = result.scalar_one()
    if product.kind != "subscription":
        raise ValueError("product must be a subscription")
    if product.lessons_count is None or product.validity_days is None:
        raise ValueError("subscription product missing lessons_count or validity_days")

    valid_until = started_on + timedelta(days=product.validity_days)
    subscription = Subscription(
        person_id=person_id,
        product_id=product_id,
        lessons_left=product.lessons_count,
        valid_until=valid_until,
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
            Subscription.person_id == person_id,
            Subscription.lessons_left > 0,
            Subscription.valid_until >= on_date,
        )
        .order_by(Subscription.valid_until.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
