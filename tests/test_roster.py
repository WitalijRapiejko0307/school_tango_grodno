import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import GroupMembership, Person
from app.services.roster import (
    claim_person,
    confirm_roster,
    find_unclaimed_by_query,
    name_key_from_parts,
    parse_roster_line,
)
from app.services.schedule import create_group


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_name_key_order_independent() -> None:
    assert name_key_from_parts("Ирина", "Вашкевич") == name_key_from_parts(
        "Вашкевич", "Ирина"
    )


def test_parse_voytevich_couple() -> None:
    drafts = parse_roster_line("Войтехович Олег и Елена")
    assert len(drafts) == 2
    assert drafts[0].full_name == "Войтехович Олег"
    assert drafts[1].full_name == "Войтехович Елена"
    assert drafts[0].name_key == name_key_from_parts("Войтехович", "Олег")
    assert drafts[1].name_key == name_key_from_parts("Войтехович", "Елена")
    assert not drafts[0].needs_fix
    assert not drafts[1].needs_fix


def test_parse_rahunok_plus_ivan() -> None:
    drafts = parse_roster_line("Рахунок Ольга +ИВАН")
    assert len(drafts) == 2
    assert drafts[0].full_name == "Рахунок Ольга"
    assert drafts[0].name_key is not None
    assert not drafts[0].needs_fix
    assert drafts[1].needs_fix
    assert drafts[1].name_key is None


def test_parse_patronymic_drops_from_key() -> None:
    drafts = parse_roster_line("Гулецкая Ирина Ивановна")
    assert len(drafts) == 1
    assert drafts[0].name_key == name_key_from_parts("Гулецкая", "Ирина")
    assert drafts[0].full_name == "Гулецкая Ирина"
    assert "Ивановна" in drafts[0].note


async def test_find_unclaimed_order_swap(db_session: AsyncSession) -> None:
    key = name_key_from_parts("Вашкевич", "Ирина")
    db_session.add(
        Person(
            full_name="Вашкевич Ирина",
            name_key=key,
            role="client",
            telegram_user_id=None,
        )
    )
    await db_session.flush()

    found = await find_unclaimed_by_query(db_session, "Ирина Вашкевич")
    assert len(found) == 1
    assert found[0].full_name == "Вашкевич Ирина"


async def test_find_unclaimed_surname_only_empty(db_session: AsyncSession) -> None:
    db_session.add(
        Person(
            full_name="Вашкевич Ирина",
            name_key=name_key_from_parts("Вашкевич", "Ирина"),
            role="client",
            telegram_user_id=None,
        )
    )
    await db_session.flush()
    assert await find_unclaimed_by_query(db_session, "Вашкевич") == []


async def test_claimed_excluded_from_search(db_session: AsyncSession) -> None:
    db_session.add(
        Person(
            full_name="Вашкевич Ирина",
            name_key=name_key_from_parts("Вашкевич", "Ирина"),
            role="client",
            telegram_user_id=9001,
        )
    )
    await db_session.flush()
    assert await find_unclaimed_by_query(db_session, "Ирина Вашкевич") == []


async def test_confirm_creates_membership(db_session: AsyncSession) -> None:
    group = await create_group(db_session, "Начинающие")
    drafts = parse_roster_line("Асадчий Владимир")
    result = await confirm_roster(db_session, group.id, drafts)
    assert result.created == 1
    assert result.skipped == 0

    person = await db_session.scalar(select(Person).where(Person.full_name == "Асадчий Владимир"))
    assert person is not None
    assert person.role == "client"
    assert person.telegram_user_id is None
    assert person.name_key == name_key_from_parts("Асадчий", "Владимир")

    membership = await db_session.scalar(
        select(GroupMembership).where(
            GroupMembership.person_id == person.id,
            GroupMembership.ended_on.is_(None),
        )
    )
    assert membership is not None
    assert membership.group_id == group.id


async def test_confirm_idempotent_in_same_group(db_session: AsyncSession) -> None:
    group = await create_group(db_session, "G")
    drafts = parse_roster_line("Козловская Дарья")
    first = await confirm_roster(db_session, group.id, drafts)
    second = await confirm_roster(db_session, group.id, drafts)
    assert first.created == 1
    assert second.skipped == 1
    assert second.created == 0

    count = await db_session.scalar(select(func.count()).select_from(Person))
    assert count == 1


async def test_claim_sets_telegram_id(db_session: AsyncSession) -> None:
    person = Person(
        full_name="Козловская Дарья",
        name_key=name_key_from_parts("Козловская", "Дарья"),
        role="client",
        telegram_user_id=None,
    )
    db_session.add(person)
    await db_session.flush()

    updated = await claim_person(
        db_session,
        person.id,
        telegram_user_id=42,
        username="darya",
        telegram_full_name="Darya TG",
    )
    assert updated.telegram_user_id == 42
    assert updated.username == "darya"
    assert updated.full_name == "Козловская Дарья"

    with pytest.raises(ValueError, match="telegram_already_linked"):
        await claim_person(
            db_session,
            person.id,
            telegram_user_id=99,
            username=None,
            telegram_full_name="Other",
        )


def test_local_ocr_line_keeps_letters_and_inner_hyphen() -> None:
    from app.services.ocr import clean_local_ocr_line

    assert clean_local_ocr_line("'|, Рапейко Виталий.") == "Рапейко Виталий"
    assert clean_local_ocr_line("12. Гулецкий Вячеслав,") == "Гулецкий Вячеслав"
    assert clean_local_ocr_line("Петров—Водкин Кузьма") == "Петров-Водкин Кузьма"
    assert clean_local_ocr_line("| _") == ""


def test_numbered_lines_replace_draft_rows_instead_of_new_list() -> None:
    from app.channels.telegram import _apply_roster_replacements, _parse_lines_to_draft_dicts

    drafts = _parse_lines_to_draft_dicts("Рапейко\nМордань")
    text = "1. Рапейко Виталий\n2. Мордань Виктория"
    updated, error = _apply_roster_replacements(drafts, text)
    assert error == ""
    assert [item["full_name"] for item in updated] == [
        "Рапейко Виталий",
        "Мордань Виктория",
    ]
    assert all(item["needs_fix"] is False for item in updated)
