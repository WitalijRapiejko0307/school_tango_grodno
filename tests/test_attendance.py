from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DropInCharge, Group, Person, SchoolSession, Subscription
from app.services.attendance import mark_attendance
from app.services.billing import create_product, open_subscription, set_price
from app.services.schedule import create_group, create_session


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


async def _person_and_session(
    db_session: AsyncSession,
    ends_at: datetime,
) -> tuple[Person, SchoolSession]:
    person = Person(full_name="Dancer", role="client")
    group = await create_group(db_session, "Main")
    db_session.add(person)
    await db_session.flush()
    school_session = await create_session(
        db_session,
        group.id,
        starts_at=ends_at.replace(hour=ends_at.hour - 1 if ends_at.hour else 18),
        ends_at=ends_at,
        place="Studio",
        bring_notes=None,
    )
    return person, school_session


async def test_admin_then_client_mark_admin_wins_single_lesson_decrement(
    db_session: AsyncSession,
) -> None:
    minsk = ZoneInfo("Europe/Minsk")
    ends = datetime(2026, 4, 10, 20, 0, tzinfo=minsk)
    person, school_session = await _person_and_session(db_session, ends)

    product = await create_product(
        db_session,
        "subscription",
        "Pack",
        lessons_count=5,
        validity_days=30,
    )
    sub = await open_subscription(
        db_session,
        person.id,
        product.id,
        started_on=date(2026, 4, 1),
    )

    admin_mark = await mark_attendance(
        db_session, person.id, school_session.id, "admin"
    )
    client_mark = await mark_attendance(
        db_session, person.id, school_session.id, "client"
    )

    assert admin_mark.id == client_mark.id
    assert client_mark.source == "admin"

    await db_session.refresh(sub)
    assert sub.lessons_left == 4


async def test_drop_in_charge_uses_price_on_session_end_date(
    db_session: AsyncSession,
) -> None:
    minsk = ZoneInfo("Europe/Minsk")
    ends = datetime(2026, 5, 15, 21, 0, tzinfo=minsk)
    person, school_session = await _person_and_session(db_session, ends)

    drop_in = await create_product(db_session, "drop_in", "Visit")
    old_price = await set_price(
        db_session, drop_in.id, Decimal("12.00"), date(2026, 1, 1)
    )
    await set_price(
        db_session, drop_in.id, Decimal("25.00"), date(2026, 6, 1)
    )

    att = await mark_attendance(
        db_session, person.id, school_session.id, "client"
    )
    assert att.source == "client"

    result = await db_session.execute(select(DropInCharge))
    charge = result.scalar_one()
    assert charge.amount == Decimal("12.00")
    assert charge.price_id == old_price.id
    assert charge.charged_at == school_session.ends_at.replace(tzinfo=None)


async def test_second_mark_does_not_duplicate_drop_in_charge(
    db_session: AsyncSession,
) -> None:
    minsk = ZoneInfo("Europe/Minsk")
    ends = datetime(2026, 6, 1, 19, 0, tzinfo=minsk)
    person, school_session = await _person_and_session(db_session, ends)

    drop_in = await create_product(db_session, "drop_in", "Visit")
    await set_price(
        db_session, drop_in.id, Decimal("10.00"), date(2026, 1, 1)
    )

    await mark_attendance(db_session, person.id, school_session.id, "client")
    await mark_attendance(db_session, person.id, school_session.id, "client")

    n = await db_session.scalar(select(func.count()).select_from(DropInCharge))
    assert n == 1


async def test_subscription_visit_no_drop_in_charge_decrements_once(
    db_session: AsyncSession,
) -> None:
    minsk = ZoneInfo("Europe/Minsk")
    ends = datetime(2026, 7, 20, 20, 30, tzinfo=minsk)
    person, school_session = await _person_and_session(db_session, ends)

    drop_in = await create_product(db_session, "drop_in", "Visit")
    await set_price(
        db_session, drop_in.id, Decimal("99.00"), date(2026, 1, 1)
    )
    product = await create_product(
        db_session,
        "subscription",
        "Sub",
        lessons_count=3,
        validity_days=90,
    )
    sub = await open_subscription(
        db_session, person.id, product.id, started_on=date(2026, 7, 1)
    )

    await mark_attendance(db_session, person.id, school_session.id, "client")
    await mark_attendance(db_session, person.id, school_session.id, "admin")

    await db_session.refresh(sub)
    assert sub.lessons_left == 2

    n = await db_session.scalar(select(func.count()).select_from(DropInCharge))
    assert n == 0
