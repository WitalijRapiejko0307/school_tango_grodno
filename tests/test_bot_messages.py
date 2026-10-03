from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.models import BotChatMessage, Person
from app.services.bot_messages import (
    ADMIN_CHAT_LIMIT,
    CLIENT_GUEST_CHAT_LIMIT,
    apply_message_deletions,
    record_bot_message,
)
from app.services.schedule import assign_group, create_group, create_session

MINSK = ZoneInfo("Europe/Minsk")


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


async def _client(db_session: AsyncSession, tg_id: int = 5001) -> Person:
    person = Person(full_name="Client", role="client", telegram_user_id=tg_id)
    group = await create_group(db_session, "G")
    db_session.add(person)
    await db_session.flush()
    await assign_group(db_session, person.id, group.id, date(2026, 1, 1))
    return person


async def test_deletes_session_linked_message_before_today(
    db_session: AsyncSession,
) -> None:
    person = await _client(db_session)
    group = await create_group(db_session, "Yesterday")
    starts = datetime(2026, 10, 1, 19, 0, tzinfo=MINSK)
    school_session = await create_session(
        db_session,
        group.id,
        starts_at=starts,
        ends_at=starts + timedelta(hours=1),
        place="Hall",
        bring_notes=None,
    )
    bot = AsyncMock()
    row = BotChatMessage(
        person_id=person.id,
        chat_id=person.telegram_user_id or 0,
        telegram_message_id=101,
        school_session_id=school_session.id,
        is_reply_menu_anchor=False,
        sent_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.flush()

    await apply_message_deletions(
        db_session, bot, person, today=date(2026, 10, 2)
    )

    bot.delete_message.assert_awaited_once_with(
        person.telegram_user_id, 101
    )
    remaining = await db_session.scalar(select(BotChatMessage))
    assert remaining is None


async def test_chat_limit_keeps_menu_anchor(db_session: AsyncSession) -> None:
    person = await _client(db_session)
    bot = AsyncMock()
    chat_id = person.telegram_user_id or 0
    base = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    for i in range(CLIENT_GUEST_CHAT_LIMIT):
        db_session.add(
            BotChatMessage(
                person_id=person.id,
                chat_id=chat_id,
                telegram_message_id=1000 + i,
                school_session_id=None,
                is_reply_menu_anchor=False,
                sent_at=base + timedelta(minutes=i),
            )
        )
    anchor = BotChatMessage(
        person_id=person.id,
        chat_id=chat_id,
        telegram_message_id=9999,
        school_session_id=None,
        is_reply_menu_anchor=True,
        sent_at=base + timedelta(minutes=CLIENT_GUEST_CHAT_LIMIT),
    )
    db_session.add(anchor)
    extra = BotChatMessage(
        person_id=person.id,
        chat_id=chat_id,
        telegram_message_id=8888,
        school_session_id=None,
        is_reply_menu_anchor=False,
        sent_at=base + timedelta(minutes=CLIENT_GUEST_CHAT_LIMIT + 1),
    )
    db_session.add(extra)
    await db_session.flush()

    await apply_message_deletions(
        db_session, bot, person, today=date(2026, 10, 2)
    )

    deleted_ids = {
        call.args[1] for call in bot.delete_message.await_args_list
    }
    assert 1000 in deleted_ids
    assert 9999 not in deleted_ids
    rows = list((await db_session.scalars(select(BotChatMessage))).all())
    assert len(rows) == CLIENT_GUEST_CHAT_LIMIT + 1
    assert any(r.telegram_message_id == 9999 for r in rows)


async def test_shared_limit_counts_user_and_bot_messages(
    db_session: AsyncSession,
) -> None:
    person = await _client(db_session)
    bot = AsyncMock()
    chat_id = person.telegram_user_id or 0
    base = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    for i in range(CLIENT_GUEST_CHAT_LIMIT + 1):
        db_session.add(
            BotChatMessage(
                person_id=person.id,
                chat_id=chat_id,
                telegram_message_id=3000 + i,
                school_session_id=None,
                is_reply_menu_anchor=False,
                sent_at=base + timedelta(seconds=i),
            )
        )
    await db_session.flush()

    await apply_message_deletions(
        db_session, bot, person, today=date(2026, 10, 2)
    )

    assert bot.delete_message.await_count == 1
    assert bot.delete_message.await_args.args[1] == 3000


async def test_admin_limit_is_twenty(db_session: AsyncSession) -> None:
    admin = Person(full_name="Admin", role="admin", telegram_user_id=9001)
    db_session.add(admin)
    await db_session.flush()
    bot = AsyncMock()
    base = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    for i in range(ADMIN_CHAT_LIMIT + 1):
        db_session.add(
            BotChatMessage(
                person_id=admin.id,
                chat_id=9001,
                telegram_message_id=2000 + i,
                school_session_id=None,
                is_reply_menu_anchor=False,
                sent_at=base + timedelta(seconds=i),
            )
        )
    await db_session.flush()

    await apply_message_deletions(
        db_session, bot, admin, today=date(2026, 10, 2)
    )

    assert bot.delete_message.await_count == 1
    assert bot.delete_message.await_args.args[1] == 2000


async def test_delete_failure_does_not_remove_row(db_session: AsyncSession) -> None:
    person = await _client(db_session)
    bot = AsyncMock()
    bot.delete_message.side_effect = RuntimeError("telegram down")
    row = BotChatMessage(
        person_id=person.id,
        chat_id=person.telegram_user_id or 0,
        telegram_message_id=42,
        school_session_id=None,
        is_reply_menu_anchor=False,
        sent_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.flush()

    await apply_message_deletions(
        db_session, bot, person, today=date(2026, 10, 2)
    )

    assert await db_session.get(BotChatMessage, row.id) is not None


async def test_record_clears_previous_menu_anchor(db_session: AsyncSession) -> None:
    person = await _client(db_session)
    bot = AsyncMock()
    old = BotChatMessage(
        person_id=person.id,
        chat_id=person.telegram_user_id or 0,
        telegram_message_id=1,
        is_reply_menu_anchor=True,
        sent_at=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
    )
    db_session.add(old)
    await db_session.flush()

    await record_bot_message(
        db_session,
        bot,
        person=person,
        chat_id=person.telegram_user_id or 0,
        telegram_message_id=2,
        sets_reply_menu=True,
        sent_at=datetime(2026, 10, 2, 11, 0, tzinfo=UTC),
    )

    await db_session.refresh(old)
    assert old.is_reply_menu_anchor is False
    new_row = await db_session.scalar(
        select(BotChatMessage).where(BotChatMessage.telegram_message_id == 2)
    )
    assert new_row is not None
    assert new_row.is_reply_menu_anchor is True
