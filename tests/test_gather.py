from datetime import UTC, date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DayGather, Group, GroupMembership, Person
from app.services.gather import (
    apply_gather_selection,
    in_morning_gather_window,
    morning_gather_due,
    nudge_deadline,
    nudge_is_due,
    parse_gather_indexes,
    process_due_gathers,
    validate_gather_indexes,
)
from app.services.schedule import (
    assign_group,
    create_group,
    create_stream,
    create_weekly_slot,
    set_group_stream,
)

TZ = timezone.utc


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


@pytest.mark.parametrize(
    ("class_hour", "expected_hour"),
    [
        (20, 15),
        (16, 13),
    ],
)
def test_nudge_deadline_before_class(class_hour: int, expected_hour: int) -> None:
    morning = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
    class_start = datetime(2026, 6, 1, class_hour, 0, tzinfo=TZ)
    result = nudge_deadline(morning, class_start)
    assert result == datetime(2026, 6, 1, expected_hour, 0, tzinfo=TZ)


def test_nudge_deadline_too_close_to_morning() -> None:
    morning = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
    class_start = datetime(2026, 6, 1, 11, 0, tzinfo=TZ)
    assert nudge_deadline(morning, class_start) is None


def test_nudge_catch_up_after_missed_slot_and_after_late_morning() -> None:
    morning = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
    class_start = datetime(2026, 6, 1, 16, 0, tzinfo=TZ)
    assert not nudge_is_due(morning, class_start, datetime(2026, 6, 1, 12, 0, tzinfo=TZ))
    assert nudge_is_due(morning, class_start, datetime(2026, 6, 1, 14, 0, tzinfo=TZ))
    assert not nudge_is_due(morning, class_start, class_start)

    late_morning = datetime(2026, 6, 1, 13, 30, tzinfo=TZ)
    assert nudge_deadline(late_morning, class_start) is None
    assert not nudge_is_due(
        late_morning, class_start, datetime(2026, 6, 1, 14, 0, tzinfo=TZ)
    )
    assert nudge_is_due(
        late_morning, class_start, datetime(2026, 6, 1, 14, 30, tzinfo=TZ)
    )


def test_parse_gather_indexes() -> None:
    assert parse_gather_indexes("1, 3") == [1, 3]
    assert parse_gather_indexes(" 2 , 1 ") == [2, 1]
    assert parse_gather_indexes("привет") is None
    assert parse_gather_indexes("1, 9") == [1, 9]


def test_validate_gather_indexes() -> None:
    assert validate_gather_indexes([1, 3], 3) == [1, 3]
    assert validate_gather_indexes([2, 2, 1], 3) == [2, 1]
    assert validate_gather_indexes([1, 4], 3) is None


def test_in_morning_gather_window() -> None:
    # Europe/Minsk is UTC+3 in winter
    inside = datetime(2026, 1, 15, 6, 30, tzinfo=UTC)  # 09:30 Minsk
    outside = datetime(2026, 1, 15, 7, 0, tzinfo=UTC)  # 10:00 Minsk
    assert in_morning_gather_window(inside)
    assert not in_morning_gather_window(outside)


@pytest.mark.asyncio
async def test_apply_gather_selection_parses_one_and_three(
    db_session: AsyncSession,
) -> None:
    stream = await create_stream(db_session, "Поток")
    group = await create_group(db_session, "Группа А")
    await set_group_stream(db_session, group.id, stream.id)
    weekday = date(2026, 6, 2).weekday()
    await create_weekly_slot(
        db_session,
        group.id,
        weekday,
        time(19, 0),
        time(20, 0),
        "Зал",
        None,
    )
    person = Person(full_name="Иван", role="client", telegram_user_id=1001)
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, started_on=date(2026, 1, 1))
    gather_date = date(2026, 6, 2)
    day_gather = DayGather(
        person_id=person.id,
        stream_id=stream.id,
        gather_date=gather_date,
        sent_at=datetime(2026, 6, 2, 6, 0, tzinfo=UTC),
        response="none",
    )
    db_session.add(day_gather)
    await db_session.flush()

    from app.services.gather import sessions_for_stream_on_date

    rows = await sessions_for_stream_on_date(db_session, stream.id, gather_date)
    assert len(rows) >= 1
    now = datetime(2026, 6, 2, 7, 0, tzinfo=UTC)
    ok, text = await apply_gather_selection(db_session, day_gather, [1], now)
    assert ok
    assert "Записали" in text
    assert day_gather.response == "coming"
    assert day_gather.selected_session_ids == rows[0].school_session.id


@pytest.mark.asyncio
async def test_morning_window_sends_only_once(db_session: AsyncSession) -> None:
    stream = await create_stream(db_session, "Утро")
    group = await create_group(db_session, "Группа")
    await set_group_stream(db_session, group.id, stream.id)
    # 2026-01-15 is Thursday
    await create_weekly_slot(
        db_session,
        group.id,
        3,
        time(19, 0),
        time(20, 0),
        "Студия",
        None,
    )
    person = Person(full_name="Мария", role="client", telegram_user_id=42)
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, started_on=date(2026, 1, 1))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    now = datetime(2026, 1, 15, 6, 15, tzinfo=UTC)  # 09:15 Minsk
    assert in_morning_gather_window(now)
    await process_due_gathers(db_session, bot, now)
    await process_due_gathers(db_session, bot, now)

    assert bot.send_message.await_count == 1
    result = await db_session.execute(select(DayGather))
    assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_missed_morning_hour_sends_while_class_is_ahead(
    db_session: AsyncSession,
) -> None:
    stream = await create_stream(db_session, "Вечер")
    group = await create_group(db_session, "Группа")
    await set_group_stream(db_session, group.id, stream.id)
    await create_weekly_slot(
        db_session,
        group.id,
        3,
        time(19, 0),
        time(20, 0),
        "Студия",
        None,
    )
    person = Person(full_name="Ольга", role="client", telegram_user_id=77)
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, started_on=date(2026, 1, 1))

    bot = MagicMock()
    bot.send_message = AsyncMock()
    late = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)  # 13:00 Minsk, class at 19:00
    assert morning_gather_due(late)
    assert not in_morning_gather_window(late)
    await process_due_gathers(db_session, bot, late)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_missed_morning_hour_skips_after_class_started(
    db_session: AsyncSession,
) -> None:
    stream = await create_stream(db_session, "День")
    group = await create_group(db_session, "Группа")
    await set_group_stream(db_session, group.id, stream.id)
    await create_weekly_slot(
        db_session,
        group.id,
        3,
        time(12, 0),
        time(13, 0),
        "Студия",
        None,
    )
    person = Person(full_name="Пётр", role="client", telegram_user_id=78)
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, started_on=date(2026, 1, 1))

    bot = MagicMock()
    bot.send_message = AsyncMock()
    after = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)  # 13:30 Minsk, class was 12:00
    await process_due_gathers(db_session, bot, after)
    assert bot.send_message.await_count == 0
