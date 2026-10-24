from datetime import date
from decimal import Decimal

import pytest

from app.channels.telegram.billing import (
    amount_paid_for_open_sub,
    parse_open_sub_amount,
    parse_open_sub_payment_choice,
    parse_open_sub_start_date,
    partial_amount_error,
)


def test_parse_open_sub_start_date() -> None:
    assert parse_open_sub_start_date("01.03.2026") == date(2026, 3, 1)


def test_parse_open_sub_start_date_invalid() -> None:
    with pytest.raises(ValueError):
        parse_open_sub_start_date("01-03-2026")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("да", "yes"),
        ("Да", "yes"),
        ("нет", "no"),
        ("частично", "partial"),
        ("maybe", None),
    ],
)
def test_parse_open_sub_payment_choice(text: str, expected: str | None) -> None:
    assert parse_open_sub_payment_choice(text) == expected


def test_parse_open_sub_amount() -> None:
    assert parse_open_sub_amount("120,50") == Decimal("120.50")
    assert parse_open_sub_amount("80") == Decimal("80")


def test_partial_amount_error() -> None:
    assert partial_amount_error(Decimal("50"), Decimal("100")) is None
    assert partial_amount_error(Decimal("0"), Decimal("100")) is not None
    assert partial_amount_error(Decimal("100"), Decimal("100")) is not None
    assert partial_amount_error(Decimal("10"), None) is None


def test_amount_paid_for_open_sub() -> None:
    due = Decimal("100")
    assert amount_paid_for_open_sub("yes", due) == due
    assert amount_paid_for_open_sub("no", due) == Decimal("0")
    assert amount_paid_for_open_sub("partial", due, Decimal("40")) == Decimal("40")
    assert amount_paid_for_open_sub("yes", None) == Decimal("0")
