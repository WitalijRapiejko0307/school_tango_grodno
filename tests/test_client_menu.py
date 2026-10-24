from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.channels.telegram.common import role_keyboard
from app.channels.telegram.core import (
    cb_go,
    menu_client_classes,
    menu_client_subscriptions,
)
from app.db import Base
from app.models import Person
from app.services.billing import create_product, open_subscription
from app.services.schedule import assign_group, create_group, create_session, create_weekly_slot

MINSK = ZoneInfo("Europe/Minsk")


@pytest.fixture
async def telegram_db(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.channels.telegram.core.async_session_maker", factory)
    yield factory
    await engine.dispose()


def test_client_role_keyboard() -> None:
    assert role_keyboard("client") == ["Прайс", "Абонементы", "Занятия"]


async def test_menu_client_subscriptions_lists_product(
    telegram_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date(2026, 10, 28)
    monkeypatch.setattr("app.channels.telegram.core._today_local", lambda: today)

    async with telegram_db() as session:
        client = Person(full_name="Client", role="client", telegram_user_id=42)
        session.add(client)
        await session.flush()
        product = await create_product(
            session,
            "subscription",
            "8 занятий",
            lessons_count=8,
            validity_days=60,
        )
        await open_subscription(
            session,
            client.id,
            product.id,
            started_on=today,
            starts_on=today,
            amount_due=Decimal("100"),
            amount_paid=Decimal("100"),
        )
        await session.commit()

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=42)
    message.answer = AsyncMock()

    await menu_client_subscriptions(message)

    text = message.answer.await_args.args[0]
    assert "8 занятий" in text
    assert "активен" in text


async def test_menu_client_classes_shows_week_schedule(
    telegram_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date(2026, 10, 28)  # Wednesday
    monkeypatch.setattr("app.channels.telegram.core._today_local", lambda: today)

    async with telegram_db() as session:
        client = Person(full_name="Client", role="client", telegram_user_id=7)
        session.add(client)
        group = await create_group(session, "Начинающие")
        await assign_group(session, client.id, group.id, today)
        await create_weekly_slot(
            session,
            group.id,
            today.weekday(),
            time(19, 0),
            time(20, 0),
            "зал А",
            None,
        )
        await session.commit()

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    message.answer = AsyncMock()

    await menu_client_classes(message)

    text = message.answer.await_args.args[0]
    assert "среда, 28.10.2026" in text
    assert "зал А" in text
    assert ", " in text  # date, time, place


async def test_menu_client_classes_without_group(telegram_db) -> None:
    async with telegram_db() as session:
        client = Person(full_name="Lonely", role="client", telegram_user_id=8)
        session.add(client)
        await session.commit()

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=8)
    message.answer = AsyncMock()

    await menu_client_classes(message)

    assert message.answer.await_args.args[0] == "Вы не в группе."


async def test_cb_go_appends_unpaid_subscription_note(
    telegram_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date(2026, 10, 28)
    monkeypatch.setattr("app.channels.telegram.core._today_local", lambda: today)

    async with telegram_db() as session:
        client = Person(full_name="Client", role="client", telegram_user_id=99)
        session.add(client)
        group = await create_group(session, "G")
        await session.flush()
        starts = datetime(2026, 10, 29, 18, 0, tzinfo=MINSK)
        school_session = await create_session(
            session,
            group.id,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            place="Studio",
            bring_notes=None,
        )
        product = await create_product(
            session,
            "subscription",
            "Pack",
            lessons_count=4,
            validity_days=30,
        )
        await open_subscription(
            session,
            client.id,
            product.id,
            started_on=today,
            starts_on=today,
            amount_due=Decimal("50"),
            amount_paid=Decimal("0"),
        )
        await session.commit()
        session_id = school_session.id

    query = MagicMock()
    query.data = f"go:{session_id}"
    query.from_user = SimpleNamespace(id=99)
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.answer = AsyncMock()

    await cb_go(query)

    reply = query.message.answer.await_args.args[0]
    assert "записали, что идёте" in reply
    assert "неоплаченный абонемент" in reply
