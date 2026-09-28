"""Schedule application services (groups, streams, sessions)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Group, GroupMembership, Person, SchoolSession, Stream, StreamMember


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


async def list_month_sessions(
    session: AsyncSession,
    year: int,
    month: int,
    tz_name: str | None = None,
) -> list[SchoolSession]:
    if tz_name is None:
        tz_name = get_settings().SCHOOL_TZ
    start_utc, end_utc = _month_bounds_utc(year, month, tz_name)
    result = await session.execute(
        select(SchoolSession).where(
            and_(
                SchoolSession.starts_at >= start_utc,
                SchoolSession.starts_at < end_utc,
            )
        ).order_by(SchoolSession.starts_at)
    )
    return list(result.scalars().all())
