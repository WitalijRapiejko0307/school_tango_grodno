from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import GroupMembership, Person
from app.services.roster import archive_person, restore_person
from app.services.schedule import assign_group, create_group


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


async def test_archive_sets_archived_at_and_ends_membership(
    db_session: AsyncSession,
) -> None:
    group = await create_group(db_session, "G")
    person = Person(
        full_name="Test User",
        name_key="test|user",
        role="client",
        telegram_user_id=None,
    )
    db_session.add(person)
    await db_session.flush()
    started_on = date(2026, 1, 1)
    await assign_group(db_session, person.id, group.id, started_on)

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    ended_on = date(2026, 3, 1)
    archived = await archive_person(db_session, person.id, now, ended_on)

    assert archived.archived_at == now
    membership = await db_session.scalar(
        select(GroupMembership).where(GroupMembership.person_id == person.id)
    )
    assert membership is not None
    assert membership.ended_on == ended_on


async def test_restore_clears_archived_at_keeps_membership_end(
    db_session: AsyncSession,
) -> None:
    group = await create_group(db_session, "G")
    person = Person(
        full_name="Test User",
        name_key="test|user",
        role="client",
        telegram_user_id=None,
    )
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, date(2026, 1, 1))

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    ended_on = date(2026, 3, 1)
    await archive_person(db_session, person.id, now, ended_on)

    restored = await restore_person(db_session, person.id)
    assert restored.archived_at is None

    membership = await db_session.scalar(
        select(GroupMembership).where(GroupMembership.person_id == person.id)
    )
    assert membership is not None
    assert membership.ended_on == ended_on


async def test_archive_person_missing_raises(db_session: AsyncSession) -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="person"):
        await archive_person(db_session, "missing-id", now, date(2026, 3, 1))

    with pytest.raises(ValueError, match="person"):
        await restore_person(db_session, "missing-id")
