from __future__ import annotations

from dataclasses import dataclass

PAGE_SIZE = 20

HINT_ONE = "Отправьте номер из списка или имя и фамилию целиком."
HINT_MANY = (
    "Отправьте номера через запятую или имена и фамилии целиком через запятую."
)


@dataclass(frozen=True)
class PickRow:
    id: str
    full_name: str
    detail: str = ""  # group name or status text shown after an em dash


class PickError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _page_count(rows: list[PickRow]) -> int:
    if not rows:
        return 1
    return (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE


def _format_row_line(number: int, row: PickRow) -> str:
    if row.detail:
        return f"{number}. {row.full_name} — {row.detail}"
    return f"{number}. {row.full_name}"


def page_text(rows: list[PickRow], page: int, hint: str) -> str:
    total_pages = _page_count(rows)
    if page < 0 or page >= total_pages:
        raise ValueError(f"page {page} out of range")

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    slice_rows = rows[start:end]
    lines = [_format_row_line(start + i + 1, row) for i, row in enumerate(slice_rows)]
    body = "\n".join(lines)
    if body:
        return f"{body}\n\n{hint}"
    return hint


def has_next_page(rows: list[PickRow], page: int) -> bool:
    return page + 1 < _page_count(rows)


def _resolve_one_token(rows: list[PickRow], token: str) -> PickRow:
    stripped = token.strip()
    if stripped.isdigit():
        index = int(stripped)
        if index < 1 or index > len(rows):
            raise PickError(f"Нет пункта с номером {index}.")
        return rows[index - 1]

    key = stripped.casefold()
    matches = [row for row in rows if row.full_name.casefold() == key]
    if not matches:
        raise PickError(f"Не найдено: {token.strip()}.")
    if len(matches) > 1:
        raise PickError(
            "Несколько людей с таким именем — отправьте номер из списка."
        )
    return matches[0]


def resolve_picks(rows: list[PickRow], text: str, *, many: bool) -> list[PickRow]:
    if not text or not text.strip():
        raise PickError("Пустой ввод.")

    if not many:
        return [_resolve_one_token(rows, text)]

    tokens = [part.strip() for part in text.split(",") if part.strip()]
    if not tokens:
        raise PickError("Пустой ввод.")

    seen: set[str] = set()
    result: list[PickRow] = []
    for token in tokens:
        row = _resolve_one_token(rows, token)
        if row.id not in seen:
            seen.add(row.id)
            result.append(row)
    return result


def short_report(
    action: str, names: list[str], failed: list[str] | None = None
) -> str:
    if not names:
        suffix = "" if action.endswith(".") else "."
        base = f"{action}{suffix}"
        if failed:
            failed_part = ", ".join(failed)
            return f"{base.rstrip('.')}. Не дошло: {failed_part}."
        return base

    if len(names) <= 3:
        joined = ", ".join(names)
        base = f"{action}: {joined}."
    else:
        n = len(names)
        base = f"{action} {n} людям."

    if failed:
        failed_part = ", ".join(failed)
        return f"{base} Не дошло: {failed_part}."
    return base


def attendance_report(lines: list[tuple[int, str, str]]) -> str:
    return "\n".join(
        f"{number}. {full_name} — {outcome}"
        for number, full_name, outcome in lines
    )
