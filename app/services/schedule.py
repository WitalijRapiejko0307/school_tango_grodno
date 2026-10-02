"""Schedule application services (groups, streams, weekly slots, sessions)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    Group,
    GroupMembership,
    Person,
    SchoolSession,
    Stream,
    StreamMember,
    WeeklySlot,
)

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _month_bounds_utc(year: int, month: int, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


async def create_group(
    session: AsyncSession, name: str, stream_id: str | None = None
) -> Group:
    group = Group(name=name, stream_id=stream_id)
    session.add(group)
    await session.flush()
    return group


async def set_group_stream(
    session: AsyncSession, group_id: str, stream_id: str | None
) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        raise ValueError("group")
    group.stream_id = stream_id
    await session.flush()
    return group


async def active_group_membership(
    session: AsyncSession, person_id: str
) -> GroupMembership | None:
    result = await session.execute(
        select(GroupMembership).where(
            GroupMembership.person_id == person_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    return result.scalar_one_or_none()


def group_transfer_confirmation_text(
    person_full_name: str,
    from_group_name: str,
    member_since: date,
    to_group_name: str,
) -> str:
    from app.services.school_time import format_school_date

    return (
        f"{person_full_name} уже в „{from_group_name}“ с "
        f"{format_school_date(member_since)}. Перевести в „{to_group_name}“?"
    )


async def create_stream(session: AsyncSession, name: str) -> Stream:
    stream = Stream(name=name)
    session.add(stream)
    await session.flush()
    return stream


async def add_stream_member(
    session: AsyncSession, stream_id: str, person_id: str
) -> StreamMember:
    result = await session.execute(
        select(StreamMember).where(
            StreamMember.stream_id == stream_id,
            StreamMember.person_id == person_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is not None:
        return member
    member = StreamMember(stream_id=stream_id, person_id=person_id)
    session.add(member)
    await session.flush()
    return member


async def assign_group(
    session: AsyncSession,
    person_id: str,
    group_id: str,
    started_on: date,
) -> GroupMembership:
    result = await session.execute(
        select(GroupMembership).where(
            GroupMembership.person_id == person_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    for membership in result.scalars().all():
        membership.ended_on = started_on

    membership = GroupMembership(
        group_id=group_id,
        person_id=person_id,
        started_on=started_on,
    )
    session.add(membership)

    person_result = await session.execute(
        select(Person).where(Person.id == person_id)
    )
    person = person_result.scalar_one()
    if person.role == "guest":
        person.role = "client"

    await session.flush()
    return membership


async def create_session(
    session: AsyncSession,
    group_id: str,
    starts_at: datetime,
    ends_at: datetime,
    place: str,
    bring_notes: str | None,
) -> SchoolSession:
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    school_session = SchoolSession(
        group_id=group_id,
        starts_at=starts_at,
        ends_at=ends_at,
        place=place,
        bring_notes=bring_notes,
        status="scheduled",
    )
    session.add(school_session)
    await session.flush()
    return school_session


def parse_dmy(text: str) -> date:
    return datetime.strptime(text.strip(), "%d-%m-%Y").date()


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().SCHOOL_TZ)


def _local_span(on_date: date, start: time, end: time) -> tuple[datetime, datetime]:
    tz = _tz()
    starts = datetime(
        on_date.year, on_date.month, on_date.day, start.hour, start.minute, tzinfo=tz
    )
    ends = datetime(
        on_date.year, on_date.month, on_date.day, end.hour, end.minute, tzinfo=tz
    )
    return starts.astimezone(ZoneInfo("UTC")), ends.astimezone(ZoneInfo("UTC"))


async def create_weekly_slot(
    session: AsyncSession,
    group_id: str,
    weekday: int,
    start_time: time,
    end_time: time,
    place: str,
    notes: str | None,
) -> WeeklySlot:
    if not 0 <= weekday <= 6:
        raise ValueError("weekday")
    if end_time <= start_time:
        raise ValueError("ends before start")
    slot = WeeklySlot(
        group_id=group_id,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        place=place,
        notes=notes,
        active=True,
    )
    session.add(slot)
    await session.flush()
    return slot


def _apply_slot_times(school_session: SchoolSession, slot: WeeklySlot, on_date: date) -> None:
    starts, ends = _local_span(on_date, slot.start_time, slot.end_time)
    school_session.starts_at = starts
    school_session.ends_at = ends
    school_session.place = slot.place
    school_session.bring_notes = slot.notes
    school_session.group_id = slot.group_id


async def ensure_occurrence(
    session: AsyncSession, slot: WeeklySlot, on_date: date
) -> SchoolSession:
    result = await session.execute(
        select(SchoolSession).where(
            SchoolSession.weekly_slot_id == slot.id,
            SchoolSession.session_date == on_date,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing
    starts, ends = _local_span(on_date, slot.start_time, slot.end_time)
    school_session = SchoolSession(
        group_id=slot.group_id,
        weekly_slot_id=slot.id,
        session_date=on_date,
        overridden=False,
        starts_at=starts,
        ends_at=ends,
        place=slot.place,
        bring_notes=slot.notes,
        status="scheduled",
    )
    session.add(school_session)
    await session.flush()
    return school_session


async def materialize_range(
    session: AsyncSession, start: date, end: date
) -> list[SchoolSession]:
    """Create concrete sessions for active weekly slots in [start, end)."""
    if end <= start:
        return []
    slots = list(
        (
            await session.execute(select(WeeklySlot).where(WeeklySlot.active.is_(True)))
        ).scalars().all()
    )
    created: list[SchoolSession] = []
    day = start
    while day < end:
        for slot in slots:
            if slot.weekday != day.weekday():
                continue
            created.append(await ensure_occurrence(session, slot, day))
        day += timedelta(days=1)
    return created


async def update_weekly_slot(
    session: AsyncSession,
    slot_id: str,
    weekday: int,
    start_time: time,
    end_time: time,
    place: str,
    notes: str | None,
) -> WeeklySlot:
    if end_time <= start_time:
        raise ValueError("ends before start")
    slot = await session.get(WeeklySlot, slot_id)
    if slot is None:
        raise ValueError("slot")
    slot.weekday = weekday
    slot.start_time = start_time
    slot.end_time = end_time
    slot.place = place
    slot.notes = notes
    today = datetime.now(_tz()).date()
    result = await session.execute(
        select(SchoolSession).where(
            SchoolSession.weekly_slot_id == slot.id,
            SchoolSession.overridden.is_(False),
            SchoolSession.session_date >= today,
        )
    )
    for school_session in result.scalars().all():
        on_date = school_session.session_date
        if on_date is None:
            continue
        if on_date.weekday() != weekday:
            school_session.status = "cancelled"
            continue
        _apply_slot_times(school_session, slot, on_date)
        school_session.status = "scheduled"
    await session.flush()
    return slot


async def override_occurrence(
    session: AsyncSession,
    slot_id: str,
    on_date: date,
    start_time: time,
    end_time: time,
    place: str,
    notes: str | None,
) -> SchoolSession:
    if end_time <= start_time:
        raise ValueError("ends before start")
    slot = await session.get(WeeklySlot, slot_id)
    if slot is None:
        raise ValueError("slot")
    school_session = await ensure_occurrence(session, slot, on_date)
    starts, ends = _local_span(on_date, start_time, end_time)
    school_session.starts_at = starts
    school_session.ends_at = ends
    school_session.place = place
    school_session.bring_notes = notes
    school_session.overridden = True
    school_session.status = "scheduled"
    await session.flush()
    return school_session


async def cancel_occurrence(
    session: AsyncSession, slot_id: str, on_date: date
) -> SchoolSession:
    slot = await session.get(WeeklySlot, slot_id)
    if slot is None:
        raise ValueError("slot")
    school_session = await ensure_occurrence(session, slot, on_date)
    school_session.status = "cancelled"
    school_session.overridden = True
    await session.flush()
    return school_session


async def list_month_sessions(
    session: AsyncSession,
    year: int,
    month: int,
    tz_name: str | None = None,
) -> list[SchoolSession]:
    if tz_name is None:
        tz_name = get_settings().SCHOOL_TZ
    tz = ZoneInfo(tz_name)
    month_start = datetime(year, month, 1, tzinfo=tz).date()
    if month == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=tz).date()
    else:
        month_end = datetime(year, month + 1, 1, tzinfo=tz).date()
    await materialize_range(session, month_start, month_end)
    start_utc, end_utc = _month_bounds_utc(year, month, tz_name)
    result = await session.execute(
        select(SchoolSession).where(
            and_(
                SchoolSession.starts_at >= start_utc,
                SchoolSession.starts_at < end_utc,
                SchoolSession.status == "scheduled",
            )
        ).order_by(SchoolSession.starts_at)
    )
    return list(result.scalars().all())
