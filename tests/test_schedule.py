from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Group, GroupMembership, Person, SchoolSession
from app.services.schedule import (
    assign_group,
    create_group,
    create_session,
    create_stream,
    create_weekly_slot,
    group_transfer_confirmation_text,
    list_month_sessions,
    materialize_range,
    override_occurrence,
    parse_dmy,
    set_group_stream,
    update_weekly_slot,
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


def test_group_transfer_confirmation_text_uses_school_date_format() -> None:
    text = group_transfer_confirmation_text(
        "Иван",
        "Начинающие",
        date(2026, 10, 1),
        "Продолжающие",
    )
    assert text == (
        "Иван уже в „Начинающие“ с 01.10.2026. Перевести в „Продолжающие“?"
    )


async def test_set_group_stream_updates_group(db_session: AsyncSession) -> None:
    stream = await create_stream(db_session, "Весна")
    group = await create_group(db_session, "Группа 1")
    updated = await set_group_stream(db_session, group.id, stream.id)
    assert updated.stream_id == stream.id
    await set_group_stream(db_session, group.id, None)
    row = await db_session.get(Group, group.id)
    assert row is not None
    assert row.stream_id is None


async def test_assign_group_closes_previous_membership_and_sets_role_client(
    db_session: AsyncSession,
) -> None:
    person = Person(full_name="Student", role="guest", telegram_user_id=5005)
    group_a = await create_group(db_session, "Group A")
    group_b = await create_group(db_session, "Group B")
    db_session.add(person)
    await db_session.flush()

    first = await assign_group(
        db_session, person.id, group_a.id, started_on=date(2026, 1, 1)
    )
    assert first.ended_on is None

    second = await assign_group(
        db_session, person.id, group_b.id, started_on=date(2026, 3, 1)
    )
    assert second.group_id == group_b.id
    assert second.ended_on is None

    await db_session.refresh(person)
    assert person.role == "client"

    result = await db_session.execute(
        select(GroupMembership).where(GroupMembership.id == first.id)
    )
    closed = result.scalar_one()
    assert closed.ended_on == date(2026, 3, 1)


async def test_list_month_sessions_respects_timezone_boundaries(
    db_session: AsyncSession,
) -> None:
    group = await create_group(db_session, "Tuesday")
    # February 2026 in Europe/Minsk: [Jan 31 21:00 UTC, Feb 28 21:00 UTC)
    in_feb_minsk = datetime(2026, 1, 31, 21, 0, tzinfo=timezone.utc)
    still_feb_minsk = datetime(2026, 2, 28, 20, 59, tzinfo=timezone.utc)
    in_mar_minsk = datetime(2026, 2, 28, 21, 0, tzinfo=timezone.utc)
    in_jan_minsk = datetime(2026, 1, 31, 20, 59, tzinfo=timezone.utc)

    for starts, ends in [
        (in_jan_minsk, in_jan_minsk.replace(hour=22)),
        (in_feb_minsk, in_feb_minsk.replace(hour=22, minute=30)),
        (still_feb_minsk, still_feb_minsk.replace(minute=59, second=59)),
        (in_mar_minsk, in_mar_minsk.replace(hour=22, minute=30)),
    ]:
        await create_session(
            db_session,
            group.id,
            starts,
            ends,
            place="Studio",
            bring_notes=None,
        )

    sessions = await list_month_sessions(
        db_session, year=2026, month=2, tz_name="Europe/Minsk"
    )
    assert len(sessions) == 2
    starts_list = sorted(s.starts_at for s in sessions)

    def as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    assert [as_utc(dt) for dt in starts_list] == [in_feb_minsk, still_feb_minsk]


async def test_create_session_rejects_inverted_times(
    db_session: AsyncSession,
) -> None:
    group = await create_group(db_session, "Group")
    starts = datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 4, 1, 18, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="ends_at must be after starts_at"):
        await create_session(
            db_session, group.id, starts, ends, place="Hall", bring_notes=None
        )

    result = await db_session.execute(select(SchoolSession))
    assert len(result.scalars().all()) == 0


def test_parse_dmy() -> None:
    assert parse_dmy("30-09-2026") == date(2026, 9, 30)
    with pytest.raises(ValueError):
        parse_dmy("2026-09-30")


async def test_override_one_date_is_kept_when_weekly_slot_changes(
    db_session: AsyncSession,
) -> None:
    group = await create_group(db_session, "1 группа")
    slot = await create_weekly_slot(
        db_session, group.id, 4, time(19, 0), time(20, 0), "Зал", None
    )
    today = date.today()
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    next_friday = friday + timedelta(days=7)
    await materialize_range(db_session, friday, next_friday + timedelta(days=1))
    await override_occurrence(
        db_session, slot.id, friday, time(18, 0), time(19, 0), "Другой зал", "один раз"
    )
    await update_weekly_slot(
        db_session, slot.id, 4, time(20, 0), time(21, 0), "Новый зал", None
    )
    rows = (
        await db_session.execute(
            select(SchoolSession).order_by(SchoolSession.session_date)
        )
    ).scalars().all()
    by_date = {row.session_date: row for row in rows}
    assert by_date[friday].place == "Другой зал"
    assert by_date[friday].overridden is True
    assert by_date[next_friday].place == "Новый зал"
    assert by_date[next_friday].overridden is False
