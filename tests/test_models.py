from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Attendance, Group, Person, SchoolSession


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


async def test_person_and_session_round_trip(db_session: AsyncSession) -> None:
    person = Person(full_name="Anna Client", role="client", telegram_user_id=12345)
    db_session.add(person)
    await db_session.flush()

    group = Group(name="Tuesday beginners")
    db_session.add(group)
    await db_session.flush()

    starts = datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 4, 1, 20, 30, tzinfo=timezone.utc)
    school_session = SchoolSession(
        group_id=group.id,
        starts_at=starts,
        ends_at=ends,
        place="Studio A",
        bring_notes="Comfortable shoes",
        status="scheduled",
    )
    db_session.add(school_session)
    await db_session.commit()

    result = await db_session.execute(
        select(Person).where(Person.id == person.id)
    )
    loaded_person = result.scalar_one()
    assert loaded_person.full_name == "Anna Client"
    assert loaded_person.role == "client"

    session_result = await db_session.execute(
        select(SchoolSession).where(SchoolSession.id == school_session.id)
    )
    loaded_session = session_result.scalar_one()
    assert loaded_session.place == "Studio A"
    assert loaded_session.starts_at == starts


async def test_attendance_unique_per_person_and_session(
    db_session: AsyncSession,
) -> None:
    person = Person(full_name="Bob", role="client")
    group = Group(name="Group 1")
    db_session.add_all([person, group])
    await db_session.flush()

    school_session = SchoolSession(
        group_id=group.id,
        starts_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 5, 1, 19, 30, tzinfo=timezone.utc),
        place="Hall",
        status="scheduled",
    )
    db_session.add(school_session)
    await db_session.flush()

    first = Attendance(
        person_id=person.id,
        session_id=school_session.id,
        source="admin",
        marked_at=datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc),
    )
    db_session.add(first)
    await db_session.commit()

    duplicate = Attendance(
        person_id=person.id,
        session_id=school_session.id,
        source="client",
        marked_at=datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc),
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.commit()
