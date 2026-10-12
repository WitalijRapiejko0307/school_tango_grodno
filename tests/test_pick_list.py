import pytest

from app.services.pick_list import (
    HINT_MANY,
    HINT_ONE,
    PAGE_SIZE,
    PickError,
    PickRow,
    attendance_report,
    has_next_page,
    page_text,
    resolve_picks,
    short_report,
)


def _rows(n: int) -> list[PickRow]:
    return [
        PickRow(id=str(i), full_name=f"Person {i}", detail=f"detail {i}")
        for i in range(1, n + 1)
    ]


def test_page_numbering_item_21_on_second_page() -> None:
    rows = _rows(25)
    text = page_text(rows, page=1, hint=HINT_ONE)
    assert text.startswith("21. Person 21")
    assert "20. Person 20" not in text
    assert text.endswith(HINT_ONE)
    first_page = page_text(rows, page=0, hint=HINT_ONE)
    assert "1. Person 1" in first_page
    assert f"{PAGE_SIZE}. Person {PAGE_SIZE}" in first_page
    assert "21. Person 21" not in first_page


def test_page_out_of_range_raises() -> None:
    rows = _rows(5)
    with pytest.raises(ValueError):
        page_text(rows, page=1, hint=HINT_ONE)
    with pytest.raises(ValueError):
        page_text(rows, page=-1, hint=HINT_ONE)


def test_page_omits_detail_when_empty() -> None:
    rows = [PickRow(id="1", full_name="Ivanov Ivan", detail="Beginners")]
    rows.append(PickRow(id="2", full_name="Petrova Anna", detail=""))
    text = page_text(rows, page=0, hint=HINT_ONE)
    assert "1. Ivanov Ivan — Beginners" in text
    assert "2. Petrova Anna\n" in text or "2. Petrova Anna\n\n" in text
    assert "Petrova Anna —" not in text


def test_has_next_page() -> None:
    rows = _rows(PAGE_SIZE)
    assert not has_next_page(rows, 0)
    rows = _rows(PAGE_SIZE + 1)
    assert has_next_page(rows, 0)
    assert not has_next_page(rows, 1)


def test_resolve_by_exact_number() -> None:
    rows = [
        PickRow(id="a", full_name="Ivanov Ivan"),
        PickRow(id="b", full_name="Petrova Anna"),
    ]
    assert resolve_picks(rows, "2", many=False) == [rows[1]]


def test_resolve_by_exact_name_casefold() -> None:
    rows = [PickRow(id="a", full_name="Ivanov Ivan")]
    assert resolve_picks(rows, "  ivanov ivan  ", many=False) == rows


def test_homonyms_raise_pick_error() -> None:
    rows = [
        PickRow(id="1", full_name="Ivanov Ivan"),
        PickRow(id="2", full_name="Ivanov Ivan"),
    ]
    with pytest.raises(PickError) as exc:
        resolve_picks(rows, "Ivanov Ivan", many=False)
    assert "номер" in exc.value.message.lower()


def test_name_fragment_does_not_match() -> None:
    rows = [PickRow(id="a", full_name="Ivanov Ivan")]
    with pytest.raises(PickError):
        resolve_picks(rows, "Ivanov", many=False)
    with pytest.raises(PickError):
        resolve_picks(rows, "Ivan", many=False)


def test_resolve_many_all_or_nothing() -> None:
    rows = [
        PickRow(id="a", full_name="One"),
        PickRow(id="b", full_name="Two"),
        PickRow(id="c", full_name="Three"),
    ]
    with pytest.raises(PickError):
        resolve_picks(rows, "1, bad", many=True)
    assert resolve_picks(rows, "1, 3", many=True) == [rows[0], rows[2]]


def test_resolve_many_deduplicates_by_id() -> None:
    rows = [
        PickRow(id="a", full_name="One"),
        PickRow(id="b", full_name="Two"),
    ]
    assert resolve_picks(rows, "1, 2, 1", many=True) == [rows[0], rows[1]]


def test_short_report_examples() -> None:
    assert (
        short_report("Сообщение ушло", [f"n{i}" for i in range(12)])
        == "Сообщение ушло 12 людям."
    )
    assert (
        short_report(
            "Сообщение ушло",
            [f"n{i}" for i in range(12)],
            failed=["Сидоров Пётр"],
        )
        == "Сообщение ушло 12 людям. Не дошло: Сидоров Пётр."
    )
    assert short_report("Группа теперь «Средние»", []) == "Группа теперь «Средние»."
    assert (
        short_report("Абонемент «8 занятий» выдан", ["Иванова Анна"])
        == "Абонемент «8 занятий» выдан: Иванова Анна."
    )


def test_attendance_report() -> None:
    lines = [
        (1, "Иванов Иван", "снят"),
        (2, "Петрова Anna", "отмечен"),
    ]
    assert (
        attendance_report(lines)
        == "1. Иванов Иван — снят\n2. Петрова Anna — отмечен"
    )
