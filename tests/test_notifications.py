from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Attendance, DropInCharge, Person, Reminder
from app.services.attendance import mark_attendance
from app.services.billing import create_product, open_subscription, set_price
from app.services.notifications import (
    NUDGE_AFTER_HOURS,
    answer_attendance,
    mark_sent,
    plan_due,
    record_coming,
)
from app.services.schedule import assign_group, create_group, create_session

MINSK = ZoneInfo("Europe/Minsk")


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


async def _client_in_group(
    db_session: AsyncSession,
    telegram_user_id: int = 1001,
) -> tuple[Person, str]:
    person = Person(
        full_name="Client",
        role="client",
        telegram_user_id=telegram_user_id,
    )
    group = await create_group(db_session, "Tango A")
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, date(2026, 1, 1))
    return person, group.id


async def test_session_start_inside_lead_window_not_after_start(
    db_session: AsyncSession,
) -> None:
    person, group_id = await _client_in_group(db_session)
    starts = datetime(2026, 6, 15, 20, 0, tzinfo=MINSK)
    ends = starts + timedelta(hours=1, minutes=30)
    school_session = await create_session(
        db_session,
        group_id,
        starts_at=starts,
        ends_at=ends,
        place="Hall",
        bring_notes=None,
    )

    inside = starts - timedelta(hours=2)
    due = await plan_due(db_session, inside)
    kinds = [d for d in due if d.person_id == person.id]
    assert len(kinds) == 1
    assert kinds[0].kind == "session_start"
    assert kinds[0].school_session_id == school_session.id
    assert "Tango A" in kinds[0].text

    after_start = starts + timedelta(minutes=5)
    due_after = await plan_due(db_session, after_start)
    assert not [
        d for d in due_after if d.kind == "session_start" and d.person_id == person.id
    ]


async def test_record_coming_then_attendance_ask_after_end(
    db_session: AsyncSession,
) -> None:
    person, group_id = await _client_in_group(db_session)
    starts = datetime(2026, 7, 1, 19, 0, tzinfo=MINSK)
    ends = starts + timedelta(hours=1)
    school_session = await create_session(
        db_session,
        group_id,
        starts_at=starts,
        ends_at=ends,
        place="Studio",
        bring_notes=None,
    )

    coming_at = starts - timedelta(hours=1)
    await record_coming(db_session, person.id, school_session.id, coming_at)

    after_end = ends + timedelta(minutes=1)
    due = await plan_due(db_session, after_end)
    asks = [
        d
        for d in due
        if d.person_id == person.id and d.kind == "attendance_ask"
    ]
    assert len(asks) == 1
    assert "были на занятии" in asks[0].text.lower()


async def test_answer_yes_creates_attendance_when_not_admin(
    db_session: AsyncSession,
) -> None:
    person, group_id = await _client_in_group(db_session)
    starts = datetime(2026, 8, 10, 18, 0, tzinfo=MINSK)
    ends = starts + timedelta(hours=1)
    school_session = await create_session(
        db_session,
        group_id,
        starts_at=starts,
        ends_at=ends,
        place="X",
        bring_notes=None,
    )
    db_session.add(
        Reminder(
            person_id=person.id,
            kind="attendance_ask",
            session_id=school_session.id,
        )
    )
    await db_session.flush()

    drop_in = await create_product(db_session, "drop_in", "Visit")
    await set_price(db_session, drop_in.id, Decimal("15.00"), date(2026, 1, 1))

    msg = await answer_attendance(
        db_session, person.id, school_session.id, True, ends
    )
    assert "вашему ответу" in msg.lower()

    att = await db_session.scalar(
        select(Attendance).where(
            Attendance.person_id == person.id,
            Attendance.session_id == school_session.id,
        )
    )
    assert att is not None
    assert att.source == "client"


async def test_answer_yes_when_admin_already_marked(
    db_session: AsyncSession,
) -> None:
    person, group_id = await _client_in_group(db_session)
    starts = datetime(2026, 8, 11, 18, 0, tzinfo=MINSK)
    ends = starts + timedelta(hours=1)
    school_session = await create_session(
        db_session,
        group_id,
        starts_at=starts,
        ends_at=ends,
        place="X",
        bring_notes=None,
    )
    drop_in = await create_product(db_session, "drop_in", "Visit")
    await set_price(db_session, drop_in.id, Decimal("10.00"), date(2026, 1, 1))
    await mark_attendance(db_session, person.id, school_session.id, "admin")

    db_session.add(
        Reminder(
            person_id=person.id,
            kind="attendance_ask",
            session_id=school_session.id,
        )
    )
    await db_session.flush()

    msg = await answer_attendance(
        db_session, person.id, school_session.id, True, ends
    )
    assert "администратором" in msg.lower()

    att = await db_session.scalar(
        select(Attendance).where(
            Attendance.person_id == person.id,
            Attendance.session_id == school_session.id,
        )
    )
    assert att.source == "admin"


async def test_nudge_once_after_delay(
    db_session: AsyncSession,
) -> None:
    person, group_id = await _client_in_group(db_session)
    starts = datetime(2026, 9, 1, 19, 0, tzinfo=MINSK)
    ends = starts + timedelta(hours=1)
    school_session = await create_session(
        db_session,
        group_id,
        starts_at=starts,
        ends_at=ends,
        place="X",
        bring_notes=None,
    )
    sent_at = ends + timedelta(hours=1)
    ask = Reminder(
        person_id=person.id,
        kind="attendance_ask",
        session_id=school_session.id,
        sent_at=sent_at,
        response="none",
    )
    db_session.add(ask)
    await db_session.flush()

    too_early = sent_at + timedelta(hours=NUDGE_AFTER_HOURS - 1)
    assert not [
        d for d in await plan_due(db_session, too_early) if d.kind == "attendance_nudge"
    ]

    nudge_time = sent_at + timedelta(hours=NUDGE_AFTER_HOURS, minutes=1)
    nudges = await plan_due(db_session, nudge_time)
    nudge_items = [d for d in nudges if d.kind == "attendance_nudge"]
    assert len(nudge_items) == 1
    assert nudge_items[0].reminder_id == ask.id

    await mark_sent(db_session, ask.id, nudge_time, nudge=True)

    again = await plan_due(db_session, nudge_time + timedelta(hours=5))
    assert not [d for d in again if d.kind == "attendance_nudge"]


async def test_month_drop_in_total_uses_stored_amounts(
    db_session: AsyncSession,
) -> None:
    person = Person(full_name="Dropper", role="client", telegram_user_id=42)
    group = await create_group(db_session, "G")
    db_session.add(person)
    await db_session.flush()
    product = await create_product(db_session, "drop_in", "Visit")
    price = await set_price(
        db_session, product.id, Decimal("99.99"), date(2026, 1, 1)
    )
    for day, amount in ((10, Decimal("12.50")), (20, Decimal("7.50"))):
        starts = datetime(2026, 3, day, 19, 0, tzinfo=MINSK)
        ends = starts + timedelta(hours=1)
        school_session = await create_session(
            db_session,
            group.id,
            starts_at=starts,
            ends_at=ends,
            place="P",
            bring_notes=None,
        )
        db_session.add(
            DropInCharge(
                person_id=person.id,
                session_id=school_session.id,
                amount=amount,
                price_id=price.id,
                charged_at=ends,
            )
        )
    await db_session.flush()

    now = datetime(2026, 4, 1, 10, 0, tzinfo=MINSK)
    due = await plan_due(db_session, now)
    month_items = [
        d for d in due if d.kind == "month_drop_in_total" and d.person_id == person.id
    ]
    assert len(month_items) == 1
    assert "2" in month_items[0].text
    assert "20.00" in month_items[0].text
    assert "99.99" not in month_items[0].text


async def test_subscription_low_only_at_one_lesson_and_once(
    db_session: AsyncSession,
) -> None:
    person, _ = await _client_in_group(db_session)
    product = await create_product(
        db_session,
        "subscription",
        "Pack",
        lessons_count=5,
        validity_days=30,
    )
    sub = await open_subscription(
        db_session, person.id, product.id, started_on=date(2026, 5, 1)
    )
    sub.lessons_left = 1
    await db_session.flush()

    now = datetime(2026, 5, 10, 12, 0, tzinfo=MINSK)
    due1 = await plan_due(db_session, now)
    low = [d for d in due1 if d.kind == "subscription_low" and d.person_id == person.id]
    assert len(low) == 1

    await mark_sent(db_session, low[0].reminder_id, now)

    due2 = await plan_due(db_session, now + timedelta(hours=1))
    assert not [
        d for d in due2 if d.kind == "subscription_low" and d.person_id == person.id
    ]

    sub2 = await open_subscription(
        db_session, person.id, product.id, started_on=date(2026, 6, 1)
    )
    sub2.lessons_left = 1
    await db_session.flush()
    due3 = await plan_due(db_session, datetime(2026, 6, 5, 12, 0, tzinfo=MINSK))
    lows = [
        d for d in due3 if d.kind == "subscription_low" and d.person_id == person.id
    ]
    assert len(lows) == 1
    assert lows[0].reminder_id != low[0].reminder_id
