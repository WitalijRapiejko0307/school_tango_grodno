"""Morning gather (day RSVP) helpers and due processing."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DayGather,
    DayGatherResponse,
    Group,
    GroupMembership,
    Person,
    SchoolSession,
)
from app.services.notifications import record_coming
from app.services.schedule import materialize_range
from app.services.school_time import format_school_date, format_school_time, school_tz

logger = logging.getLogger(__name__)

WEEKDAY_FULL_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_GATHER_REPLY_RE = re.compile(r"^\s*\d+(?:\s*,\s*\d+)*\s*$")


def nudge_deadline(
    morning_sent_at: datetime, earliest_session_start: datetime
) -> datetime | None:
    """Planned nudge time: six hours after the morning message, not later than three hours before class."""
    if morning_sent_at.tzinfo is None or earliest_session_start.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")

    candidate = morning_sent_at + timedelta(hours=6)
    latest = earliest_session_start - timedelta(hours=3)
    nudge_at = min(candidate, latest)
    if nudge_at <= morning_sent_at:
        return None
    return nudge_at


def nudge_is_due(
    morning_sent_at: datetime, earliest_session_start: datetime, now: datetime
) -> bool:
    """True when a non-responder should get the repeat now.

    The planned moment is six hours after the morning message, capped at three
    hours before class. If that moment was missed, the repeat still goes out
    until the class starts. A morning message sent after the cap gets the
    repeat an hour later, still before the class.
    """
    if now >= earliest_session_start:
        return False
    deadline = nudge_deadline(morning_sent_at, earliest_session_start)
    if deadline is not None:
        return now >= deadline
    return now >= morning_sent_at + timedelta(hours=1)


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=school_tz())
    return dt


def local_school_date(now: datetime) -> date:
    return _as_aware(now).astimezone(school_tz()).date()


def in_morning_gather_window(now: datetime) -> bool:
    """True during 09:00–09:59:59 school local time."""
    local = _as_aware(now).astimezone(school_tz())
    return local.hour == 9


def morning_gather_due(now: datetime) -> bool:
    """True from 09:00 school local time until the end of that day.

    The first attempt is the 09:00 hour. Later checks send the same message
    immediately if that hour was missed.
    """
    local = _as_aware(now).astimezone(school_tz())
    return local.hour >= 9


def parse_gather_indexes(text: str) -> list[int] | None:
    """Parse «1, 3» style replies; None if the text is not a number list."""
    if not _GATHER_REPLY_RE.match(text):
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return [int(p) for p in parts]


def validate_gather_indexes(indexes: list[int], session_count: int) -> list[int] | None:
    """Return 1-based indices if all valid; None if any index is out of range."""
    if session_count < 1:
        return None
    seen: set[int] = set()
    ordered: list[int] = []
    for idx in indexes:
        if idx < 1 or idx > session_count:
            return None
        if idx not in seen:
            seen.add(idx)
            ordered.append(idx)
    return ordered


@dataclass(frozen=True)
class GatherSessionRow:
    school_session: SchoolSession
    group_name: str


def format_gather_session_line(
    index: int, row: GatherSessionRow, on_date: date
) -> str:
    weekday = WEEKDAY_FULL_RU[on_date.weekday()]
    date_str = format_school_date(on_date)
    time_str = format_school_time(row.school_session.starts_at)
    return (
        f"{index}. {weekday}, {date_str}, {time_str}, "
        f"{row.group_name}, {row.school_session.place}"
    )


def build_gather_message(full_name: str, lines: list[str], *, nudge: bool = False) -> str:
    body = "\n".join(lines)
    if nudge:
        return (
            f"{full_name}, напоминаем про занятия сегодня:\n\n"
            f"{body}\n\n"
            "Напишите номера тех, на которые придёте, через запятую."
        )
    return (
        f"Доброе утро, {full_name}! Сегодня занятия:\n\n"
        f"{body}\n\n"
        "На какие придёте? Ответьте номерами через запятую."
    )


def gather_inline_markup(day_gather_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Не иду",
                    callback_data=f"gather_no:{day_gather_id}",
                )
            ]
        ]
    )


async def _sessions_on_date(
    session: AsyncSession, gather_date: date
) -> list[tuple[SchoolSession, Group]]:
    tz = school_tz()
    day_start = datetime(gather_date.year, gather_date.month, gather_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    start_utc = day_start.astimezone(UTC)
    end_utc = day_end.astimezone(UTC)
    await materialize_range(session, gather_date, gather_date + timedelta(days=1))
    result = await session.execute(
        select(SchoolSession, Group)
        .join(Group, SchoolSession.group_id == Group.id)
        .where(
            SchoolSession.starts_at >= start_utc,
            SchoolSession.starts_at < end_utc,
            SchoolSession.status == "scheduled",
            Group.archived_at.is_(None),
        )
        .order_by(SchoolSession.starts_at)
    )
    return list(result.all())


async def sessions_for_person_on_date(
    session: AsyncSession, person_id: str, gather_date: date
) -> list[GatherSessionRow]:
    memberships = await session.execute(
        select(GroupMembership.group_id).where(
            GroupMembership.person_id == person_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    group_ids = set(memberships.scalars().all())
    rows: list[GatherSessionRow] = []
    for school_session, group in await _sessions_on_date(session, gather_date):
        if group.id not in group_ids:
            continue
        rows.append(
            GatherSessionRow(school_session=school_session, group_name=group.name)
        )
    return rows


async def _eligible_persons_today(
    session: AsyncSession, gather_date: date
) -> list[Person]:
    group_ids = [group.id for _, group in await _sessions_on_date(session, gather_date)]
    if not group_ids:
        return []
    result = await session.execute(
        select(Person)
        .join(GroupMembership, GroupMembership.person_id == Person.id)
        .where(
            GroupMembership.group_id.in_(group_ids),
            GroupMembership.ended_on.is_(None),
            Person.telegram_user_id.isnot(None),
            Person.archived_at.is_(None),
        )
        .distinct()
    )
    return list(result.scalars().all())


async def _existing_day_gather(
    session: AsyncSession,
    person_id: str,
    gather_date: date,
) -> DayGather | None:
    result = await session.execute(
        select(DayGather).where(
            DayGather.person_id == person_id,
            DayGather.gather_date == gather_date,
        )
    )
    return result.scalar_one_or_none()


async def _send_gather_message(
    session: AsyncSession,
    bot: Bot,
    person: Person,
    gather_date: date,
    session_rows: list[GatherSessionRow],
    *,
    nudge: bool,
    day_gather: DayGather | None = None,
) -> DayGather | None:
    if person.telegram_user_id is None or not session_rows:
        return None
    lines = [
        format_gather_session_line(i + 1, row, gather_date)
        for i, row in enumerate(session_rows)
    ]
    text = build_gather_message(person.full_name, lines, nudge=nudge)
    if day_gather is None:
        day_gather = DayGather(
            person_id=person.id,
            gather_date=gather_date,
            response=DayGatherResponse.NONE,
        )
        session.add(day_gather)
        await session.flush()
    try:
        await bot.send_message(
            person.telegram_user_id,
            text,
            reply_markup=gather_inline_markup(day_gather.id),
        )
    except Exception:
        logger.exception(
            "Failed to send gather to person %s", person.id
        )
        if day_gather.sent_at is None:
            await session.delete(day_gather)
            await session.flush()
        return None
    if day_gather.sent_at is None:
        day_gather.sent_at = datetime.now(UTC)
        await session.flush()
    return day_gather


async def apply_gather_selection(
    session: AsyncSession,
    day_gather: DayGather,
    indexes: list[int],
    now: datetime,
) -> tuple[bool, str]:
    """Apply numeric reply. Returns (ok, user_message)."""
    rows = await sessions_for_person_on_date(
        session, day_gather.person_id, day_gather.gather_date
    )
    validated = validate_gather_indexes(indexes, len(rows))
    if validated is None:
        return (
            False,
            "Не получилось разобрать номера. Пришлите номера из списка, например: 1, 3",
        )
    selected_ids: list[str] = []
    chosen_lines: list[str] = []
    for idx in validated:
        row = rows[idx - 1]
        selected_ids.append(row.school_session.id)
        chosen_lines.append(
            format_gather_session_line(idx, row, day_gather.gather_date)
        )
    day_gather.response = DayGatherResponse.COMING
    day_gather.selected_session_ids = ",".join(selected_ids)
    for sid in selected_ids:
        await record_coming(session, day_gather.person_id, sid, now)
    await session.flush()
    body = "\n".join(chosen_lines)
    return True, f"Записали:\n{body}"


async def find_pending_day_gather(
    session: AsyncSession, person_id: str, gather_date: date
) -> DayGather | None:
    result = await session.execute(
        select(DayGather)
        .where(
            DayGather.person_id == person_id,
            DayGather.gather_date == gather_date,
            DayGather.response == DayGatherResponse.NONE,
            DayGather.sent_at.isnot(None),
        )
        .order_by(DayGather.sent_at.desc())
    )
    return result.scalars().first()


def _gather_status_ru(day_gather: DayGather, chosen: str) -> str:
    if day_gather.response == DayGatherResponse.COMING:
        return f"идёт: {chosen}" if chosen else "идёт"
    if day_gather.response == DayGatherResponse.DECLINED:
        return "не идёт"
    if day_gather.nudge_sent_at is not None:
        return "ответа нет, повтор отправлен"
    return "ответа нет, повтор ещё не отправлен"


async def list_today_gather_summary(
    session: AsyncSession, day: date
) -> list[tuple[DayGather, Person, str]]:
    """Morning gather rows for the admin reminders screen on ``day``."""
    result = await session.execute(
        select(DayGather, Person)
        .join(Person, DayGather.person_id == Person.id)
        .where(DayGather.gather_date == day)
        .order_by(Person.full_name)
    )
    rows: list[tuple[DayGather, Person, str]] = []
    for day_gather, person in result.all():
        chosen = ""
        if day_gather.selected_session_ids:
            ids = [
                part.strip()
                for part in day_gather.selected_session_ids.split(",")
                if part.strip()
            ]
            if ids:
                sessions = await session.execute(
                    select(SchoolSession, Group)
                    .join(Group, SchoolSession.group_id == Group.id)
                    .where(SchoolSession.id.in_(ids))
                )
                parts = [
                    f"{format_school_time(school_session.starts_at)} «{group.name}»"
                    for school_session, group in sessions.all()
                ]
                chosen = ", ".join(parts)
        detail = _gather_status_ru(day_gather, chosen)
        rows.append((day_gather, person, detail))
    return rows


async def process_due_gathers(
    session: AsyncSession, bot: Bot, now: datetime
) -> None:
    gather_date = local_school_date(now)

    if morning_gather_due(now):
        for person in await _eligible_persons_today(session, gather_date):
            if await _existing_day_gather(session, person.id, gather_date) is not None:
                continue
            session_rows = await sessions_for_person_on_date(
                session, person.id, gather_date
            )
            if not session_rows:
                continue
            if all(
                _as_aware(row.school_session.starts_at) <= _as_aware(now)
                for row in session_rows
            ):
                continue
            await _send_gather_message(
                session,
                bot,
                person,
                gather_date,
                session_rows,
                nudge=False,
            )

    nudge_result = await session.execute(
        select(DayGather).where(
            DayGather.gather_date == gather_date,
            DayGather.response == DayGatherResponse.NONE,
            DayGather.nudge_sent_at.is_(None),
            DayGather.sent_at.isnot(None),
        )
    )
    for day_gather in nudge_result.scalars().all():
        session_rows = await sessions_for_person_on_date(
            session, day_gather.person_id, gather_date
        )
        if not session_rows:
            continue
        upcoming = [
            row
            for row in session_rows
            if _as_aware(row.school_session.starts_at) > _as_aware(now)
        ]
        if not upcoming:
            continue
        earliest = min(
            _as_aware(row.school_session.starts_at) for row in upcoming
        )
        sent_at = day_gather.sent_at
        if sent_at is None:
            continue
        if not nudge_is_due(_as_aware(sent_at), earliest, _as_aware(now)):
            continue
        person = await session.get(Person, day_gather.person_id)
        if person is None or person.telegram_user_id is None:
            continue
        lines = [
            format_gather_session_line(i + 1, row, gather_date)
            for i, row in enumerate(session_rows)
        ]
        text = build_gather_message(person.full_name, lines, nudge=True)
        try:
            await bot.send_message(
                person.telegram_user_id,
                text,
                reply_markup=gather_inline_markup(day_gather.id),
            )
            day_gather.nudge_sent_at = now
            await session.flush()
        except Exception:
            logger.exception("Failed to send gather nudge %s", day_gather.id)
