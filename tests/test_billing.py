from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Person, Price
from app.services.billing import (
    create_product,
    current_price,
    list_active_products_with_prices,
    open_subscription,
    set_price,
)


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def test_price_change_preserves_history_and_current_price_by_date(
    db_session: AsyncSession,
) -> None:
    product = await create_product(
        db_session, "drop_in", "Single visit"
    )
    first = await set_price(
        db_session, product.id, Decimal("15.00"), date(2026, 1, 1)
    )
    second = await set_price(
        db_session, product.id, Decimal("18.00"), date(2026, 6, 1)
    )

    result = await db_session.execute(select(Price).order_by(Price.valid_from))
    rows = list(result.scalars().all())
    assert len(rows) == 2
    assert rows[0].id == first.id
    assert rows[0].amount == Decimal("15.00")
    assert rows[1].id == second.id
    assert rows[1].amount == Decimal("18.00")

    assert (
        await current_price(db_session, product.id, date(2026, 3, 15))
    ).amount == Decimal("15.00")
    assert (
        await current_price(db_session, product.id, date(2026, 6, 1))
    ).amount == Decimal("18.00")
    assert (
        await current_price(db_session, product.id, date(2026, 12, 1))
    ).amount == Decimal("18.00")


async def test_list_active_products_with_prices_skips_unpriced(
    db_session: AsyncSession,
) -> None:
    priced = await create_product(db_session, "drop_in", "Visit")
    unpriced = await create_product(db_session, "drop_in", "Future")
    await set_price(
        db_session, priced.id, Decimal("20.00"), date(2026, 1, 1)
    )

    items = await list_active_products_with_prices(db_session, date(2026, 4, 1))
    product_ids = {p.id for p, _ in items}
    assert priced.id in product_ids
    assert unpriced.id not in product_ids
    assert len(items) == 1
    assert items[0][1].amount == Decimal("20.00")


async def test_open_subscription_sets_lessons_and_validity(
    db_session: AsyncSession,
) -> None:
    person = Person(full_name="Client", role="client")
    db_session.add(person)
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "8 lessons / 60 days",
        lessons_count=8,
        validity_days=60,
    )
    sub = await open_subscription(
        db_session, person.id, product.id, started_on=date(2026, 1, 1)
    )
    assert sub.lessons_left == 8
    assert sub.valid_until == date(2026, 3, 2)
