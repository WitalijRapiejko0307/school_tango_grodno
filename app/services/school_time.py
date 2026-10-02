"""School-local display of datetimes (storage remains UTC)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def school_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().SCHOOL_TZ)


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=school_tz())
    return dt


def format_school_datetime(dt: datetime) -> str:
    """User-facing instant: DD.MM.YYYY HH:MM in SCHOOL_TZ."""
    local = _as_aware(dt).astimezone(school_tz())
    return local.strftime("%d.%m.%Y %H:%M")


def format_school_date(d: date) -> str:
    """User-facing calendar date: DD.MM.YYYY."""
    return d.strftime("%d.%m.%Y")


def format_school_time(dt: datetime) -> str:
    """User-facing clock time HH:MM in SCHOOL_TZ."""
    local = _as_aware(dt).astimezone(school_tz())
    return local.strftime("%H:%M")
