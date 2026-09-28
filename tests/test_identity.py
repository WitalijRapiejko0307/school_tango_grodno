import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import Base
from app.models import AdminInvite, Person
from app.services.identity import assign_client, invite_admin, upsert_from_telegram


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
