"""Roster parsing, search, confirmation, and guest claim."""

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GroupMembership, Person
from app.services.schedule import assign_group

_PATRONYMIC_ENDINGS = ("вич", "вна", "ична", "овна")
_PLUS_SUFFIX_RE = re.compile(r"\s*\+\s*(\S+)\s*$", re.UNICODE)


def normalize_token(token: str) -> str:
    """Lowercase, ё→е, strip punctuation from token edges."""
    t = token.strip().lower().replace("ё", "е")
    t = re.sub(r"^[^\w]+|[^\w]+$", "", t, flags=re.UNICODE)
    return t


def name_key_from_parts(first: str, second: str) -> str:
    """Stable key: sorted pair of normalized tokens joined by |."""
    pair = sorted([normalize_token(first), normalize_token(second)])
    return f"{pair[0]}|{pair[1]}"


def _is_patronymic(token: str) -> bool:
    low = normalize_token(token)
    return any(low.endswith(end) for end in _PATRONYMIC_ENDINGS)


def _tokens_from_line(line: str) -> list[str]:
    return [p for p in line.split() if p]


def _name_key_for_tokens(tokens: list[str]) -> str | None:
    if len(tokens) < 2:
        return None
    if len(tokens) == 2:
        return name_key_from_parts(tokens[0], tokens[1])
    if len(tokens) == 3 and _is_patronymic(tokens[2]):
        return name_key_from_parts(tokens[0], tokens[1])
    return None


def _draft_from_two_tokens(
    tokens: list[str], note: str = ""
) -> "RosterDraftItem":
    key = _name_key_for_tokens(tokens)
    if len(tokens) == 3 and _is_patronymic(tokens[2]):
        full_name = f"{tokens[0]} {tokens[1]}"
        note = note or f"отчество: {tokens[2]}"
    elif len(tokens) >= 2:
        full_name = f"{tokens[0]} {tokens[1]}"
    else:
        full_name = tokens[0] if tokens else ""
    return RosterDraftItem(
        full_name=full_name,
        name_key=key,
        needs_fix=key is None,
        note=note,
    )


@dataclass
class RosterDraftItem:
    full_name: str
    name_key: str | None
    needs_fix: bool
    note: str


@dataclass
class AdminContact:
    username: str | None
    full_name: str
    phone: str | None


@dataclass
class ConfirmRosterResult:
    created: int
    skipped: int


def parse_roster_line(raw: str) -> list[RosterDraftItem]:
    line = raw.strip()
    if not line:
        return []
    if "Ф.И." in line or line.startswith("Начинающие"):
        return []

    plus_match = _PLUS_SUFFIX_RE.search(line)
    extras: list[RosterDraftItem] = []
    if plus_match:
        plus_name = plus_match.group(1)
        line = line[: plus_match.start()].strip()
        display = plus_name[:1].upper() + plus_name[1:].lower() if plus_name else plus_name
        extras.append(
            RosterDraftItem(
                full_name=display,
                name_key=None,
                needs_fix=True,
                note="указано без фамилии",
            )
        )

    if not line:
        return extras

    tokens = _tokens_from_line(line)
    if len(tokens) == 1:
        return [
            RosterDraftItem(
                full_name=tokens[0],
                name_key=None,
                needs_fix=True,
                note="нужна фамилия и имя",
            ),
            *extras,
        ]

    if len(tokens) == 4 and tokens[2].lower() == "и":
        surname, first_name, _, second_name = tokens
        main = [
            _draft_from_two_tokens([surname, first_name]),
            _draft_from_two_tokens([surname, second_name]),
        ]
        return main + extras

    return [_draft_from_two_tokens(tokens)] + extras


def name_key_from_query(text: str) -> str | None:
    tokens = _tokens_from_line(text.strip())
    if len(tokens) == 1:
        return None
    if len(tokens) == 2:
        return name_key_from_parts(tokens[0], tokens[1])
    if len(tokens) == 3 and _is_patronymic(tokens[2]):
        return name_key_from_parts(tokens[0], tokens[1])
    return None


async def find_unclaimed_by_query(
    session: AsyncSession, text: str
) -> list[Person]:
    key = name_key_from_query(text)
    if key is None:
        return []
    result = await session.execute(
        select(Person)
        .where(
            Person.name_key == key,
            Person.telegram_user_id.is_(None),
        )
        .order_by(Person.full_name)
    )
    return list(result.scalars().all())


async def admin_contacts(session: AsyncSession) -> list[AdminContact]:
    result = await session.execute(
        select(Person)
        .where(Person.role == "admin")
        .order_by(Person.full_name)
    )
    return [
        AdminContact(
            username=p.username,
            full_name=p.full_name,
            phone=p.phone,
        )
        for p in result.scalars().all()
    ]


async def _active_membership_in_group(
    session: AsyncSession, person_id: str, group_id: str
) -> GroupMembership | None:
    result = await session.execute(
        select(GroupMembership).where(
            GroupMembership.person_id == person_id,
            GroupMembership.group_id == group_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _any_active_membership(
    session: AsyncSession, person_id: str
) -> GroupMembership | None:
    result = await session.execute(
        select(GroupMembership).where(
            GroupMembership.person_id == person_id,
            GroupMembership.ended_on.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def confirm_roster(
    session: AsyncSession,
    group_id: str,
    items: list[RosterDraftItem],
) -> ConfirmRosterResult:
    created = 0
    skipped = 0
    started_on = date.today()

    for item in items:
        if item.needs_fix or not item.name_key:
            skipped += 1
            continue

        result = await session.execute(
            select(Person).where(
                Person.name_key == item.name_key,
                Person.telegram_user_id.is_(None),
            )
        )
        person = result.scalar_one_or_none()

        if person is not None:
            if await _active_membership_in_group(session, person.id, group_id):
                skipped += 1
                continue
            if await _any_active_membership(session, person.id):
                skipped += 1
                continue
            await assign_group(session, person.id, group_id, started_on)
            created += 1
            continue

        person = Person(
            full_name=item.full_name,
            name_key=item.name_key,
            role="client",
            telegram_user_id=None,
        )
        session.add(person)
        await session.flush()
        await assign_group(session, person.id, group_id, started_on)
        created += 1

    await session.flush()
    return ConfirmRosterResult(created=created, skipped=skipped)


async def claim_person(
    session: AsyncSession,
    person_id: str,
    telegram_user_id: int,
    username: str | None,
    telegram_full_name: str,
) -> Person:
    del telegram_full_name
    person = await session.get(Person, person_id)
    if person is None:
        raise ValueError("person")
    if person.telegram_user_id is not None:
        raise ValueError("telegram_already_linked")
    person.telegram_user_id = telegram_user_id
    person.username = username
    if person.role != "admin":
        person.role = "client"
    await session.flush()
    return person
