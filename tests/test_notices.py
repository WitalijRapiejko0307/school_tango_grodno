from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import GuestRsvp, GuestRsvpStatus, Person
from app.services.notices import (
    cancel_notice_text,
    cancel_session,
    countable,
    notice_audience,
    reschedule_notice_text,
    reschedule_session,
)
from app.services.schedule import assign_group, create_group, create_session


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_notice_texts_use_school_format() -> None:
    starts = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)
    later = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    moved = reschedule_notice_text("Начинающие", starts, "зал", later, "зал 2")
    assert "04.10.2026 19:00" in moved
    assert "05.10.2026 18:00" in moved
    cancelled = cancel_notice_text("Начинающие", starts, "зал")
    assert "отменено" in cancelled
    assert "04.10.2026 19:00" in cancelled


async def test_reschedule_and_cancel_keep_override(db_session: AsyncSession) -> None:
    group = await create_group(db_session, "Начинающие")
    starts = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)
    school_session = await create_session(
        db_session, group.id, starts, starts + timedelta(hours=1), "зал", None
    )
    member = Person(full_name="Ирина Вашкевич", role="client", telegram_user_id=1)
    guest = Person(full_name="Гость", role="guest", telegram_user_id=None)
    db_session.add_all([member, guest])
    await db_session.flush()
    await assign_group(db_session, member.id, group.id, starts.date())
    db_session.add(
        GuestRsvp(
            person_id=guest.id,
            session_id=school_session.id,
            status=GuestRsvpStatus.PLANNED,
        )
    )
    await db_session.flush()

    audience = await notice_audience(db_session, school_session.id)
    reachable, missing = countable(audience)
    assert {person.id for person in audience} == {member.id, guest.id}
    assert len(reachable) == 1
    assert missing == 1

    new_starts = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
    updated, old_starts, old_place = await reschedule_session(
        db_session, school_session.id, new_starts, "зал 2"
    )
    assert old_place == "зал"
    assert old_starts == starts
    assert updated.overridden is True
    assert updated.place == "зал 2"
    assert updated.ends_at - updated.starts_at == timedelta(hours=1)

    await cancel_session(db_session, school_session.id)
    assert updated.status == "cancelled"
