"""Session changes and free-text notices, plus who should receive them."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Group, GroupMembership, GuestRsvp, GuestRsvpStatus, Person, SchoolSession
from app.services.school_time import format_school_datetime


def reschedule_notice_text(
    group_name: str,
    old_starts: datetime,
    old_place: str,
    new_starts: datetime,
    new_place: str,
) -> str:
    return (
        f"Занятие группы „{group_name}“ перенесено. "
        f"Было {format_school_datetime(old_starts)}, {old_place}. "
        f"Стало {format_school_datetime(new_starts)}, {new_place}."
    )


def cancel_notice_text(group_name: str, starts: datetime, place: str) -> str:
    return (
        f"Занятие группы „{group_name}“ "
        f"{format_school_datetime(starts)}, {place}, отменено."
    )


def free_notice_text(body: str) -> str:
    return f"Сообщение школы:\n{body.strip()}"


def delivery_report(delivered: int, failed: int) -> str:
    return f"Доставлено: {delivered}. Не дошло: {failed}."


async def upcoming_sessions(
    session: AsyncSession, now: datetime
) -> list[tuple[SchoolSession, str]]:
    result = await session.execute(
        select(SchoolSession, Group.name)
        .join(Group, Group.id == SchoolSession.group_id)
        .where(
            SchoolSession.starts_at >= now,
            SchoolSession.status == "scheduled",
        )
        .order_by(SchoolSession.starts_at)
    )
    return list(result.all())


async def notice_audience(session: AsyncSession, school_session_id: str) -> list[Person]:
    school_session = await session.get(SchoolSession, school_session_id)
    if school_session is None:
        return []
    members = await session.execute(
        select(Person)
        .join(GroupMembership, GroupMembership.person_id == Person.id)
        .where(
            GroupMembership.group_id == school_session.group_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    guests = await session.execute(
        select(Person)
        .join(GuestRsvp, GuestRsvp.person_id == Person.id)
        .where(
            GuestRsvp.session_id == school_session_id,
            GuestRsvp.status.in_(
                (GuestRsvpStatus.PLANNED, GuestRsvpStatus.REMINDED)
            ),
        )
    )
    by_id: dict[str, Person] = {}
    for person in list(members.scalars().all()) + list(guests.scalars().all()):
        by_id[person.id] = person
    return list(by_id.values())


async def group_audience(session: AsyncSession, group_id: str) -> list[Person]:
    result = await session.execute(
        select(Person)
        .join(GroupMembership, GroupMembership.person_id == Person.id)
        .where(
            GroupMembership.group_id == group_id,
            GroupMembership.ended_on.is_(None),
        )
        .order_by(Person.full_name)
    )
    return list(result.scalars().all())


async def reschedule_session(
    session: AsyncSession,
    school_session_id: str,
    new_starts_local: datetime,
    place: str,
) -> tuple[SchoolSession, datetime, str]:
    school_session = await session.get(SchoolSession, school_session_id)
    if school_session is None or school_session.status != "scheduled":
        raise ValueError("session")
    if new_starts_local.tzinfo is None:
        raise ValueError("timezone")
    duration = school_session.ends_at - school_session.starts_at
    if duration <= timedelta(0):
        duration = timedelta(hours=1)
    old_starts = school_session.starts_at
    old_place = school_session.place
    new_starts = new_starts_local.astimezone(ZoneInfo("UTC"))
    school_session.starts_at = new_starts
    school_session.ends_at = new_starts + duration
    school_session.place = place.strip()
    school_session.session_date = new_starts_local.astimezone(
        ZoneInfo(get_settings().SCHOOL_TZ)
    ).date()
    school_session.overridden = True
    await session.flush()
    return school_session, old_starts, old_place


async def cancel_session(session: AsyncSession, school_session_id: str) -> SchoolSession:
    school_session = await session.get(SchoolSession, school_session_id)
    if school_session is None or school_session.status != "scheduled":
        raise ValueError("session")
    school_session.status = "cancelled"
    school_session.overridden = True
    await session.flush()
    return school_session


def countable(people: list[Person]) -> tuple[list[Person], int]:
    """People who can be written to, and how many have no Telegram."""
    reachable = [person for person in people if person.telegram_user_id is not None]
    missing = len(people) - len(reachable)
    return reachable, missing
