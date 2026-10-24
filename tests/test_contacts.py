from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ContactCard
from app.models import Person
from app.services.contacts import (
    archive_contact,
    contact_for_person,
    create_contact,
    list_contacts,
    restore_contact,
    update_contact,
)


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


async def test_update_contact(db_session: AsyncSession) -> None:
    card = ContactCard(name="Anna", phone="+375 29 111", role_label="Teacher")
    db_session.add(card)
    await db_session.flush()

    updated = await update_contact(
        db_session, card.id, "  Anna K  ", " 37529111222 ", " Lead "
    )
    assert updated.name == "Anna K"
    assert updated.phone == "37529111222"
    assert updated.role_label == "Lead"


async def test_update_contact_rejects_empty_fields(db_session: AsyncSession) -> None:
    card = ContactCard(name="Bob", phone="123", role_label="DJ")
    db_session.add(card)
    await db_session.flush()

    with pytest.raises(ValueError, match="required"):
        await update_contact(db_session, card.id, "   ", "123", "DJ")


async def test_create_contact_with_person_id(db_session: AsyncSession) -> None:
    person = Person(full_name="Admin", role="admin")
    db_session.add(person)
    await db_session.flush()

    card = await create_contact(
        db_session, "Admin", "+375", "Директор", person_id=person.id
    )
    assert card.person_id == person.id

    found = await contact_for_person(db_session, person.id)
    assert found is not None
    assert found.id == card.id

    updated = await update_contact(
        db_session, card.id, "Admin", "123", "Boss", person_id=person.id
    )
    assert updated.person_id == person.id


async def test_archive_restore_and_list_contacts(db_session: AsyncSession) -> None:
    active = ContactCard(name="Zara", phone="1", role_label="A")
    hidden = ContactCard(name="Mila", phone="2", role_label="B")
    db_session.add_all([active, hidden])
    await db_session.flush()

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    archived = await archive_contact(db_session, hidden.id, now)
    assert archived.archived_at == now

    active_list = await list_contacts(db_session, archived=False)
    assert [c.name for c in active_list] == ["Zara"]

    archived_list = await list_contacts(db_session, archived=True)
    assert [c.name for c in archived_list] == ["Mila"]

    restored = await restore_contact(db_session, hidden.id)
    assert restored.id == hidden.id
    assert restored.archived_at is None

    active_list_after = await list_contacts(db_session, archived=False)
    assert [c.name for c in active_list_after] == ["Mila", "Zara"]
