from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.models import AdminInvite, GroupMembership, Person
from app.services.identity import assign_client, demote_admin, invite_admin, upsert_from_telegram
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


async def test_new_telegram_user_is_guest(db_session: AsyncSession) -> None:
    person = await upsert_from_telegram(
        db_session, telegram_user_id=1001, username="alice", full_name="Alice"
    )
    assert person.role == "guest"
    assert person.telegram_user_id == 1001
    assert person.username == "alice"


async def test_bootstrap_id_becomes_admin(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.identity.get_settings",
        lambda: Settings(BOOTSTRAP_ADMIN_TELEGRAM_ID=4242),
    )
    person = await upsert_from_telegram(
        db_session, telegram_user_id=4242, username=None, full_name="Bootstrap"
    )
    assert person.role == "admin"


async def test_admin_invite_by_username_pending_then_activates_on_upsert(
    db_session: AsyncSession,
) -> None:
    admin = Person(
        full_name="Admin", role="admin", telegram_user_id=1, username="admin"
    )
    db_session.add(admin)
    await db_session.flush()

    invite = await invite_admin(db_session, admin, username="FutureAdmin", phone=None)
    assert invite.status == "pending"
    assert invite.person_id is None

    person = await upsert_from_telegram(
        db_session,
        telegram_user_id=2002,
        username="@FutureAdmin",
        full_name="Future Admin",
    )
    assert person.role == "admin"

    result = await db_session.execute(
        select(AdminInvite).where(AdminInvite.id == invite.id)
    )
    updated_invite = result.scalar_one()
    assert updated_invite.status == "active"
    assert updated_invite.person_id == person.id


async def test_phone_invite_matches_different_formatting(
    db_session: AsyncSession,
) -> None:
    admin = Person(full_name="Admin", role="admin", telegram_user_id=1, username="admin")
    db_session.add(admin)
    await db_session.flush()

    invite = await invite_admin(db_session, admin, username=None, phone="+375 (44) 724-72-72")
    assert invite.status == "pending"
    assert invite.phone == "375447247272"

    person = await upsert_from_telegram(
        db_session,
        telegram_user_id=2003,
        username="lavka",
        full_name="Guest",
        phone="375447247272",
    )
    assert person.role == "admin"
    assert person.phone == "375447247272"


async def test_non_admin_cannot_invite(db_session: AsyncSession) -> None:
    guest = Person(full_name="Guest", role="guest", telegram_user_id=3003)
    db_session.add(guest)
    await db_session.flush()

    with pytest.raises(PermissionError):
        await invite_admin(db_session, guest, username="someone", phone=None)


async def test_assign_client_sets_role_but_preserves_admin(
    db_session: AsyncSession,
) -> None:
    guest = Person(full_name="G", role="guest", telegram_user_id=4004)
    admin = Person(full_name="A", role="admin", telegram_user_id=4005)
    db_session.add_all([guest, admin])
    await db_session.flush()

    updated_guest = await assign_client(db_session, guest.id)
    assert updated_guest.role == "client"

    updated_admin = await assign_client(db_session, admin.id)
    assert updated_admin.role == "admin"


async def test_demote_admin_to_client_when_active_group_member(
    db_session: AsyncSession,
) -> None:
    actor = Person(full_name="Actor", role="admin", telegram_user_id=5001)
    target = Person(full_name="Target", role="admin", telegram_user_id=5002)
    db_session.add_all([actor, target])
    await db_session.flush()

    group = await create_group(db_session, "Beginners")
    db_session.add(
        GroupMembership(
            group_id=group.id,
            person_id=target.id,
            started_on=date(2026, 1, 1),
            ended_on=None,
        )
    )
    await db_session.flush()

    demoted = await demote_admin(db_session, actor.id, target.id)
    assert demoted.id == target.id
    assert demoted.role == "client"


async def test_demote_admin_to_guest_without_active_membership(
    db_session: AsyncSession,
) -> None:
    actor = Person(full_name="Actor", role="admin", telegram_user_id=6001)
    target = Person(full_name="Target", role="admin", telegram_user_id=6002)
    db_session.add_all([actor, target])
    await db_session.flush()

    demoted = await demote_admin(db_session, actor.id, target.id)
    assert demoted.role == "guest"


async def test_demote_admin_rejects_self(db_session: AsyncSession) -> None:
    admin = Person(full_name="Solo", role="admin", telegram_user_id=7001)
    db_session.add(admin)
    await db_session.flush()

    with pytest.raises(ValueError, match="yourself"):
        await demote_admin(db_session, admin.id, admin.id)


async def test_demote_admin_rejects_last_admin(db_session: AsyncSession) -> None:
    only_admin = Person(full_name="Only", role="admin", telegram_user_id=8001)
    client = Person(full_name="Client", role="client", telegram_user_id=8002)
    db_session.add_all([only_admin, client])
    await db_session.flush()

    with pytest.raises(ValueError, match="last admin"):
        await demote_admin(db_session, client.id, only_admin.id)
