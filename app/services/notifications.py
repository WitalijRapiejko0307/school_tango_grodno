"""Reminder planning: journal rows and outbound message payloads (no channel send)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    Attendance,
    DropInCharge,
    Group,
    GroupMembership,
    GuestRsvp,
    Person,
    Reminder,
    SchoolSession,
    Subscription,
)
from app.services.attendance import mark_attendance

START_LEAD_HOURS = 3
NUDGE_AFTER_HOURS = 2
GUEST_MORNING_HOUR = 9


@dataclass
class DueReminder:
    reminder_id: str
    person_id: str
    telegram_user_id: int | None
    kind: str
    text: str
    school_session_id: str | None


def _school_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().SCHOOL_TZ)


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_school_tz())
    return dt


def _to_local(dt: datetime) -> datetime:
    return _as_aware(dt).astimezone(_school_tz())


def _naive_local(dt: datetime) -> datetime:
    return _to_local(dt).replace(tzinfo=None)


def _format_local_time(dt: datetime) -> str:
    local = _to_local(dt)
    return local.strftime("%d.%m.%Y %H:%M")


def _local_date(dt: datetime) -> date:
    return _to_local(dt).date()


def _local_day_at_hour(d: date, hour: int) -> datetime:
    tz = _school_tz()
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=tz)


def _same_local_month(a: datetime, b: datetime) -> bool:
    la, lb = _to_local(a), _to_local(b)
    return la.year == lb.year and la.month == lb.month


def _previous_calendar_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = _to_local(now)
    first_this = datetime(local.year, local.month, 1, tzinfo=_school_tz())
    if local.month == 1:
        prev_start = datetime(local.year - 1, 12, 1, tzinfo=_school_tz())
    else:
        prev_start = datetime(local.year, local.month - 1, 1, tzinfo=_school_tz())
    return prev_start, first_this


async def _already_marked(
    session: AsyncSession, person_id: str, school_session_id: str
) -> bool:
    existing = await session.scalar(
        select(Attendance.id).where(
            Attendance.person_id == person_id,
            Attendance.session_id == school_session_id,
        )
    )
    return existing is not None


async def _find_reminder(
    session: AsyncSession,
    person_id: str,
    kind: str,
    *,
    session_id: str | None = None,
    subscription_id: str | None = None,
) -> Reminder | None:
    clauses = [
        Reminder.person_id == person_id,
        Reminder.kind == kind,
    ]
    if session_id is not None:
        clauses.append(Reminder.session_id == session_id)
    if subscription_id is not None:
        clauses.append(Reminder.subscription_id == subscription_id)
    result = await session.execute(select(Reminder).where(and_(*clauses)))
    return result.scalar_one_or_none()


async def _person_telegram(
    session: AsyncSession, person_id: str
) -> tuple[Person, int | None]:
    result = await session.execute(select(Person).where(Person.id == person_id))
    person = result.scalar_one()
    return person, person.telegram_user_id


def _due_from_reminder(
    reminder: Reminder,
    person: Person,
    text: str,
    *,
    kind: str | None = None,
) -> DueReminder:
    return DueReminder(
        reminder_id=reminder.id,
        person_id=reminder.person_id,
        telegram_user_id=person.telegram_user_id,
        kind=kind or reminder.kind,
        text=text,
        school_session_id=reminder.session_id,
    )


async def record_coming(
    session: AsyncSession,
    person_id: str,
    school_session_id: str,
    now: datetime,
) -> Reminder:
    reminder = await _find_reminder(
        session,
        person_id,
        "session_start",
        session_id=school_session_id,
    )
    if reminder is None:
        reminder = Reminder(
            person_id=person_id,
            kind="session_start",
            session_id=school_session_id,
        )
        session.add(reminder)
    reminder.response = "coming"
    if reminder.sent_at is None:
        reminder.sent_at = now
    await session.flush()
    return reminder


async def plan_due(session: AsyncSession, now: datetime) -> list[DueReminder]:
    from app.services.schedule import materialize_range

    local_today = _to_local(now).date()
    await materialize_range(session, local_today, local_today + timedelta(days=2))

    due: list[DueReminder] = []
    lead = timedelta(hours=START_LEAD_HOURS)
    nudge_after = timedelta(hours=NUDGE_AFTER_HOURS)

    sessions_result = await session.execute(
        select(SchoolSession, Group)
        .join(Group, SchoolSession.group_id == Group.id)
        .where(SchoolSession.status == "scheduled")
    )
    for school_session, group in sessions_result.all():
        starts_at = _as_aware(school_session.starts_at)
        if now < starts_at - lead or now >= starts_at:
            continue
        memberships = await session.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == school_session.group_id,
                GroupMembership.ended_on.is_(None),
            )
        )
        for membership in memberships.scalars().all():
            reminder = await _find_reminder(
                session,
                membership.person_id,
                "session_start",
                session_id=school_session.id,
            )
            if reminder is None:
                reminder = Reminder(
                    person_id=membership.person_id,
                    kind="session_start",
                    session_id=school_session.id,
                )
                session.add(reminder)
                await session.flush()
            if reminder.sent_at is not None:
                continue
            person, _ = await _person_telegram(session, membership.person_id)
            text = (
                f"Занятие группы «{group.name}»: {_format_local_time(school_session.starts_at)}, "
                f"место — {school_session.place}."
            )
            due.append(_due_from_reminder(reminder, person, text))

    coming_result = await session.execute(
        select(Reminder, SchoolSession)
        .join(SchoolSession, Reminder.session_id == SchoolSession.id)
        .where(
            Reminder.kind == "session_start",
            Reminder.response == "coming",
        )
    )
    for start_reminder, school_session in coming_result.all():
        if _as_aware(school_session.ends_at) > _as_aware(now):
            continue
        if await _already_marked(session, start_reminder.person_id, school_session.id):
            continue
        existing = await _find_reminder(
            session,
            start_reminder.person_id,
            "attendance_ask",
            session_id=school_session.id,
        )
        if existing is not None:
            if existing.sent_at is None:
                person, _ = await _person_telegram(session, existing.person_id)
                due.append(_due_from_reminder(existing, person, "Вы были на занятии?"))
            continue
        ask = Reminder(
            person_id=start_reminder.person_id,
            kind="attendance_ask",
            session_id=school_session.id,
        )
        session.add(ask)
        await session.flush()
        person, _ = await _person_telegram(session, ask.person_id)
        due.append(_due_from_reminder(ask, person, "Вы были на занятии?"))

    nudge_result = await session.execute(
        select(Reminder).where(
            Reminder.kind == "attendance_ask",
            Reminder.sent_at.is_not(None),
            Reminder.nudge_sent_at.is_(None),
            Reminder.response.in_(("none",)),
        )
    )
    for ask in nudge_result.scalars().all():
        sent_at = _as_aware(ask.sent_at)
        if ask.sent_at is None or sent_at > _as_aware(now) - nudge_after:
            continue
        if ask.session_id and await _already_marked(session, ask.person_id, ask.session_id):
            continue
        person, _ = await _person_telegram(session, ask.person_id)
        text = (
            "Напоминаем: ответьте, были ли вы на занятии — "
            "так мы спишем занятие с абонемента или посчитаем разовый визит."
        )
        due.append(_due_from_reminder(ask, person, text, kind="attendance_nudge"))

    subs_result = await session.execute(
        select(Subscription).where(Subscription.lessons_left == 1)
    )
    for sub in subs_result.scalars().all():
        existing = await _find_reminder(
            session,
            sub.person_id,
            "subscription_low",
            subscription_id=sub.id,
        )
        if existing is not None:
            if existing.sent_at is None:
                person, _ = await _person_telegram(session, sub.person_id)
                due.append(
                    _due_from_reminder(
                        existing,
                        person,
                        "По абонементу осталось одно занятие.",
                    )
                )
            continue
        reminder = Reminder(
            person_id=sub.person_id,
            kind="subscription_low",
            subscription_id=sub.id,
        )
        session.add(reminder)
        await session.flush()
        person, _ = await _person_telegram(session, sub.person_id)
        due.append(
            _due_from_reminder(
                reminder,
                person,
                "По абонементу осталось одно занятие.",
            )
        )

    local_now = _to_local(now)
    if local_now.day == 1 and local_now.hour >= GUEST_MORNING_HOUR:
        prev_start, prev_end = _previous_calendar_month_bounds(now)
        prev_lo = _naive_local(prev_start)
        prev_hi = _naive_local(prev_end)
        charges_result = await session.execute(
            select(DropInCharge.person_id).where(
                DropInCharge.charged_at >= prev_lo,
                DropInCharge.charged_at < prev_hi,
            ).distinct()
        )
        for (person_id,) in charges_result.all():
            month_existing = await session.execute(
                select(Reminder).where(
                    Reminder.person_id == person_id,
                    Reminder.kind == "month_drop_in_total",
                )
            )
            already = False
            for row in month_existing.scalars().all():
                if _same_local_month(row.created_at, now):
                    already = True
                    if row.sent_at is None:
                        person, _ = await _person_telegram(session, person_id)
                        totals = await _drop_in_totals_for_person(
                            session, person_id, prev_lo, prev_hi
                        )
                        due.append(
                            _due_from_reminder(
                                row,
                                person,
                                _month_drop_in_text(*totals),
                            )
                        )
                    break
            if already:
                continue
            totals = await _drop_in_totals_for_person(
                session, person_id, prev_lo, prev_hi
            )
            if totals[0] == 0:
                continue
            reminder = Reminder(
                person_id=person_id,
                kind="month_drop_in_total",
                session_id=None,
            )
            session.add(reminder)
            await session.flush()
            person, _ = await _person_telegram(session, person_id)
            due.append(
                _due_from_reminder(
                    reminder,
                    person,
                    _month_drop_in_text(*totals),
                )
            )

    rsvp_result = await session.execute(
        select(GuestRsvp, SchoolSession)
        .join(SchoolSession, GuestRsvp.session_id == SchoolSession.id)
        .where(
            GuestRsvp.status == "planned",
            SchoolSession.status == "scheduled",
        )
    )
    for rsvp, school_session in rsvp_result.all():
        if _as_aware(now) >= _as_aware(school_session.starts_at):
            continue
        if not _guest_day_of_window(now, school_session.starts_at):
            continue
        reminder = await _find_reminder(
            session,
            rsvp.person_id,
            "guest_day_of",
            session_id=school_session.id,
        )
        if reminder is None:
            reminder = Reminder(
                person_id=rsvp.person_id,
                kind="guest_day_of",
                session_id=school_session.id,
            )
            session.add(reminder)
            await session.flush()
        if reminder.sent_at is not None:
            continue
        person, _ = await _person_telegram(session, rsvp.person_id)
        notes = school_session.bring_notes or "—"
        text = (
            f"Сегодня занятие в {_format_local_time(school_session.starts_at)}. "
            f"Место: {school_session.place}. Что взять: {notes}."
        )
        due.append(_due_from_reminder(reminder, person, text))

    return due


def _guest_day_of_window(now: datetime, starts_at: datetime) -> bool:
    local_start = _to_local(starts_at)
    aware_now = _as_aware(now)
    aware_start = _as_aware(starts_at)
    lead = timedelta(hours=START_LEAD_HOURS)
    if local_start.hour < GUEST_MORNING_HOUR:
        return aware_now >= aware_start - lead and aware_now < aware_start
    morning = _local_day_at_hour(local_start.date(), GUEST_MORNING_HOUR)
    return aware_now >= morning and aware_now < aware_start


async def _drop_in_totals_for_person(
    session: AsyncSession,
    person_id: str,
    start: datetime,
    end: datetime,
) -> tuple[int, Decimal]:
    result = await session.execute(
        select(DropInCharge).where(
            DropInCharge.person_id == person_id,
            DropInCharge.charged_at >= start,
            DropInCharge.charged_at < end,
        )
    )
    charges = list(result.scalars().all())
    total = sum((c.amount for c in charges), Decimal("0"))
    return len(charges), total


def _month_drop_in_text(count: int, total: Decimal) -> str:
    return (
        f"Итог за прошлый месяц: разовых визитов — {count}, "
        f"сумма — {total:.2f}."
    )


async def mark_sent(
    session: AsyncSession,
    reminder_id: str,
    now: datetime,
    *,
    nudge: bool = False,
) -> None:
    result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    reminder = result.scalar_one()
    if nudge:
        if reminder.nudge_sent_at is None:
            reminder.nudge_sent_at = now
    elif reminder.sent_at is None:
        reminder.sent_at = now
    if reminder.kind == "guest_day_of" and reminder.session_id is not None:
        rsvp_result = await session.execute(
            select(GuestRsvp).where(
                GuestRsvp.person_id == reminder.person_id,
                GuestRsvp.session_id == reminder.session_id,
            )
        )
        rsvp = rsvp_result.scalar_one_or_none()
        if rsvp is not None:
            rsvp.status = "reminded"
    await session.flush()


async def answer_attendance(
    session: AsyncSession,
    person_id: str,
    school_session_id: str,
    yes: bool,
    now: datetime,
) -> str:
    reminder = await _find_reminder(
        session,
        person_id,
        "attendance_ask",
        session_id=school_session_id,
    )
    if reminder is None:
        raise ValueError("attendance_ask reminder not found")

    if not yes:
        reminder.response = "attended_no"
        await session.flush()
        return "Занятие не списываем."

    from app.models import Attendance

    att_before = await session.execute(
        select(Attendance).where(
            Attendance.person_id == person_id,
            Attendance.session_id == school_session_id,
        )
    )
    existing = att_before.scalar_one_or_none()
    if existing is not None and existing.source == "admin":
        reminder.response = "attended_yes"
        await session.flush()
        return "Уже отмечено администратором."

    await mark_attendance(session, person_id, school_session_id, "client")
    reminder.response = "attended_yes"
    await session.flush()
    return "Отметили по вашему ответу."


async def guest_rsvp(
    session: AsyncSession,
    person_id: str,
    school_session_id: str,
) -> GuestRsvp:
    result = await session.execute(
        select(GuestRsvp).where(
            GuestRsvp.person_id == person_id,
            GuestRsvp.session_id == school_session_id,
        )
    )
    rsvp = result.scalar_one_or_none()
    if rsvp is not None:
        return rsvp
    rsvp = GuestRsvp(
        person_id=person_id,
        session_id=school_session_id,
        status="planned",
    )
    session.add(rsvp)
    await session.flush()
    return rsvp


async def list_reminders(session: AsyncSession, limit: int = 30) -> list[Reminder]:
    result = await session.execute(
        select(Reminder).order_by(Reminder.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def guest_day_summary(
    session: AsyncSession,
    day: date,
) -> list[tuple[GuestRsvp, Person, SchoolSession]]:
    tz = _school_tz()
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    result = await session.execute(
        select(GuestRsvp, Person, SchoolSession)
        .join(Person, GuestRsvp.person_id == Person.id)
        .join(SchoolSession, GuestRsvp.session_id == SchoolSession.id)
        .where(
            SchoolSession.starts_at >= day_start,
            SchoolSession.starts_at < day_end,
        )
        .order_by(SchoolSession.starts_at)
    )
    return list(result.all())
