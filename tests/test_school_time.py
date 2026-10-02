from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.services.school_time import format_school_date, format_school_datetime

MINSK = ZoneInfo("Europe/Minsk")


def test_format_school_datetime_from_utc() -> None:
    utc = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
    assert format_school_datetime(utc) == "02.10.2026 19:00"


def test_format_school_datetime_naive_uses_school_tz() -> None:
    naive = datetime(2026, 10, 2, 19, 0)
    assert format_school_datetime(naive) == "02.10.2026 19:00"


def test_format_school_date() -> None:
    assert format_school_date(date(2026, 10, 2)) == "02.10.2026"
