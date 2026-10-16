from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Person, Price, Subscription
from app.services.billing import (
    active_subscription,
    create_product,
    current_price,
    deactivate_product,
    list_active_products_with_prices,
    open_subscription,
    restore_product,
    set_price,
    update_product,
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
    assert sub.valid_until is None


async def test_update_product_does_not_change_subscription_lessons_left(
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
    sub.lessons_left = 3
    await db_session.flush()

    updated = await update_product(
        db_session,
        product.id,
        " 12 lessons / 90 days ",
        lessons_count=12,
        validity_days=90,
    )
    assert updated.name == "12 lessons / 90 days"
    assert updated.lessons_count == 12
    assert updated.validity_days == 90

    result = await db_session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )
    row = result.scalar_one()
    assert row.lessons_left == 3


async def test_deactivate_and_restore_product_active_listing(
    db_session: AsyncSession,
) -> None:
    product = await create_product(db_session, "drop_in", "Visit")
    await set_price(
        db_session, product.id, Decimal("20.00"), date(2026, 1, 1)
    )
    on_date = date(2026, 4, 1)

    assert product.id in {
        p.id for p, _ in await list_active_products_with_prices(db_session, on_date)
    }

    deactivated = await deactivate_product(db_session, product.id)
    assert deactivated.active is False
    assert deactivated.id == product.id
    assert not {
        p.id
        for p, _ in await list_active_products_with_prices(db_session, on_date)
    }

    restored = await restore_product(db_session, product.id)
    assert restored.active is True
    assert restored.id == product.id
    items = await list_active_products_with_prices(db_session, on_date)
    assert len(items) == 1
    assert items[0][0].id == product.id


async def test_create_product_couple_subscription_stores_audience(
    db_session: AsyncSession,
) -> None:
    product = await create_product(
        db_session,
        "subscription",
        "Couple pack",
        lessons_count=10,
        validity_days=90,
        audience="couple",
    )
    assert product.audience == "couple"


async def test_open_couple_subscription_without_partner_raises(
    db_session: AsyncSession,
) -> None:
    person = Person(full_name="Primary", role="client")
    db_session.add(person)
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "Couple",
        lessons_count=8,
        validity_days=60,
        audience="couple",
    )
    with pytest.raises(ValueError, match="partner_id"):
        await open_subscription(
            db_session, person.id, product.id, started_on=date(2026, 1, 1)
        )


async def test_open_person_subscription_with_partner_raises(
    db_session: AsyncSession,
) -> None:
    primary = Person(full_name="Primary", role="client")
    partner = Person(full_name="Partner", role="client")
    db_session.add_all([primary, partner])
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "Solo",
        lessons_count=8,
        validity_days=60,
    )
    with pytest.raises(ValueError, match="must not have a partner"):
        await open_subscription(
            db_session,
            primary.id,
            product.id,
            started_on=date(2026, 1, 1),
            partner_id=partner.id,
        )


async def test_open_couple_subscription_success(
    db_session: AsyncSession,
) -> None:
    primary = Person(full_name="Primary", role="client")
    partner = Person(full_name="Partner", role="client")
    db_session.add_all([primary, partner])
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "Couple pack",
        lessons_count=12,
        validity_days=90,
        audience="couple",
    )
    sub = await open_subscription(
        db_session,
        primary.id,
        product.id,
        started_on=date(2026, 3, 1),
        partner_id=partner.id,
    )
    assert sub.person_id == primary.id
    assert sub.partner_id == partner.id
    assert sub.lessons_left == 12
    assert sub.valid_until is None


async def test_active_subscription_finds_couple_for_partner(
    db_session: AsyncSession,
) -> None:
    primary = Person(full_name="Primary", role="client")
    partner = Person(full_name="Partner", role="client")
    db_session.add_all([primary, partner])
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "Couple",
        lessons_count=6,
        validity_days=30,
        audience="couple",
    )
    sub = await open_subscription(
        db_session,
        primary.id,
        product.id,
        started_on=date(2026, 4, 1),
        partner_id=partner.id,
    )

    found = await active_subscription(db_session, partner.id, date(2026, 4, 15))
    assert found is not None
    assert found.id == sub.id


@pytest.mark.parametrize("second_holder", ["primary", "partner"])
async def test_second_active_couple_subscription_raises(
    db_session: AsyncSession,
    second_holder: str,
) -> None:
    primary = Person(full_name="Primary", role="client")
    partner = Person(full_name="Partner", role="client")
    other = Person(full_name="Other", role="client")
    db_session.add_all([primary, partner, other])
    await db_session.flush()

    product = await create_product(
        db_session,
        "subscription",
        "Couple A",
        lessons_count=8,
        validity_days=60,
        audience="couple",
    )
    await open_subscription(
        db_session,
        primary.id,
        product.id,
        started_on=date(2026, 5, 1),
        partner_id=partner.id,
    )

    product_b = await create_product(
        db_session,
        "subscription",
        "Couple B",
        lessons_count=4,
        validity_days=30,
        audience="couple",
    )
    if second_holder == "primary":
        new_primary, new_partner = primary.id, other.id
    else:
        new_primary, new_partner = other.id, partner.id

    with pytest.raises(ValueError, match="active couple subscription"):
        await open_subscription(
            db_session,
            new_primary,
            product_b.id,
            started_on=date(2026, 5, 2),
            partner_id=new_partner,
        )
