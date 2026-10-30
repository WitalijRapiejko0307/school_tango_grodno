from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.channels.telegram.common import _list_pick_more_callback
from app.channels.telegram.core import cb_list_pick_more  # noqa: F401
from app.db import Base
from app.models import Group, Person
from app.services.billing import (
    create_product,
    delete_subscription,
    open_subscription,
    record_payment,
    update_subscription,
)
from app.services.purge import (
    NotArchivedError,
    deleted_label,
    purge_entity,
)


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _person(session: AsyncSession, name: str) -> Person:
    person = Person(full_name=name, role="client")
    session.add(person)
    await session.flush()
    return person


async def test_purge_requires_archived(db_session: AsyncSession) -> None:
    person = await _person(db_session, "Active")
    with pytest.raises(NotArchivedError):
        await purge_entity(db_session, "person", person.id)


async def test_purge_keeps_dependents_with_label(db_session: AsyncSession) -> None:
    person = await _person(db_session, "Ivan Petrov")
    product = await create_product(db_session, "subscription", "Month", 8, 30)
    sub = await open_subscription(
        db_session, person.id, product.id, date(2026, 1, 1), amount_due=Decimal("50")
    )
    person.archived_at = datetime.now(UTC)
    await db_session.flush()

    await purge_entity(db_session, "person", person.id)

    assert await db_session.get(Person, person.id) is None
    await db_session.refresh(sub)
    assert sub.person_id == person.id
    assert await deleted_label(db_session, "person", person.id) == (
        "Ivan Petrov (клиент удалён)"
    )


async def test_purge_group_only_when_archived(db_session: AsyncSession) -> None:
    group = Group(name="G1")
    db_session.add(group)
    await db_session.flush()
    with pytest.raises(NotArchivedError):
        await purge_entity(db_session, "group", group.id)
    group.archived_at = datetime.now(UTC)
    await db_session.flush()
    assert await purge_entity(db_session, "group", group.id) == "G1"


async def test_delete_subscription_blocked_by_payments(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session, "A")
    product = await create_product(db_session, "subscription", "Month", 8, 30)
    paid = await open_subscription(
        db_session, person.id, product.id, date(2026, 1, 1), amount_due=Decimal("50")
    )
    await record_payment(db_session, paid.id, Decimal("10"), date(2026, 1, 2))
    with pytest.raises(ValueError):
        await delete_subscription(db_session, paid.id)

    free = await open_subscription(
        db_session, person.id, product.id, date(2026, 2, 1), amount_due=Decimal("50")
    )
    await delete_subscription(db_session, free.id)


async def test_update_subscription_rejects_due_below_paid(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session, "A")
    product = await create_product(db_session, "subscription", "Month", 8, 30)
    sub = await open_subscription(
        db_session, person.id, product.id, date(2026, 1, 1), amount_due=Decimal("50")
    )
    await record_payment(db_session, sub.id, Decimal("30"), date(2026, 1, 2))
    with pytest.raises(ValueError):
        await update_subscription(
            db_session,
            sub.id,
            starts_on=sub.starts_on,
            valid_until=sub.valid_until,
            lessons_left=5,
            amount_due=Decimal("20"),
        )
    updated = await update_subscription(
        db_session,
        sub.id,
        starts_on=sub.starts_on,
        valid_until=sub.valid_until,
        lessons_left=5,
        amount_due=Decimal("40"),
    )
    assert updated.lessons_left == 5 and updated.amount_due == Decimal("40")


def test_more_callbacks_for_registered_lists() -> None:
    for purpose in ("gsess", "visit_create_grp", "sub_partner"):
        assert _list_pick_more_callback(purpose) == f"{purpose}_more"
    # multi-pick lists share one callback
    for purpose in ("guests", "reminders", "msg"):
        assert _list_pick_more_callback(purpose) == "msg_more"
