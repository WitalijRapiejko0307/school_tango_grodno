from datetime import date, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.channels.telegram import (
    cb_once_cancel,
    fsm_week_notes,
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
    send_due_reminder,
)
from app.config import get_settings
from app.db import Base
from app.models import Person
from app.services.notifications import DueReminder
from app.services.schedule import assign_group, create_group, create_weekly_slot


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def telegram_db(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.channels.telegram.legacy.async_session_maker", factory)
    yield factory
    await engine.dispose()


async def _fsm_context(user_id: int = 9001) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(
        storage=storage,
        key=MagicMock(user_id=user_id, chat_id=user_id, bot_id=1),
    )


async def _seed_group_slot_member(
    factory: async_sessionmaker[AsyncSession],
    *,
    admin_tg: int = 9001,
    member_tg: int = 1001,
    on_date: date = date(2026, 10, 14),
):
    async with factory() as session:
        admin = Person(full_name="Admin", role="admin", telegram_user_id=admin_tg)
        member = Person(full_name="Member", role="client", telegram_user_id=member_tg)
        session.add_all([admin, member])
        group = await create_group(session, "Начинающие")
        await assign_group(session, member.id, group.id, on_date)
        slot = await create_weekly_slot(
            session,
            group.id,
            on_date.weekday(),
            time(16, 0),
            time(17, 0),
            "зал",
            None,
        )
        await session.commit()
        return slot.id, on_date


async def test_cb_once_cancel_notifies_group_audience(telegram_db) -> None:
    slot_id, on_date = await _seed_group_slot_member(telegram_db)
    state = await _fsm_context()
    await state.update_data(slot_id=slot_id, once_date=on_date.strftime("%d-%m-%Y"))

    bot = AsyncMock()
    message = AsyncMock()
    query = MagicMock()
    query.from_user = SimpleNamespace(id=9001)
    query.bot = bot
    query.message = message
    query.answer = AsyncMock()

    await cb_once_cancel(query, state)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1001
    assert "отменено" in bot.send_message.await_args.args[1]
    message.answer.assert_awaited_once()
    admin_reply = message.answer.await_args.args[0]
    assert "отменено" in admin_reply
    assert "Доставлено:" in admin_reply


async def test_fsm_week_notes_edit_one_notifies_reschedule(telegram_db) -> None:
    slot_id, on_date = await _seed_group_slot_member(telegram_db)
    state = await _fsm_context()
    await state.update_data(
        mode="edit_one",
        slot_id=slot_id,
        once_date=on_date.strftime("%d-%m-%Y"),
        slot_time="18:00",
        slot_duration="60",
        slot_place="зал 2",
    )

    bot = AsyncMock()
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=9001)
    message.bot = bot
    message.text = "—"
    message.answer = AsyncMock()

    await fsm_week_notes(message, state)

    bot.send_message.assert_awaited_once()
    notice = bot.send_message.await_args.args[1]
    assert "перенесено" in notice
    assert "Было" in notice
    assert "Стало" in notice
    assert "зал 2" in notice
    admin_reply = message.answer.await_args.args[0]
    assert "перенесено" in admin_reply
    assert "Доставлено:" in admin_reply


async def test_health_ok() -> None:
    from app.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_webhook_without_token_returns_503() -> None:
    from app.main import app

    get_settings.cache_clear()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/telegram/webhook", json={})
    assert response.status_code == 503


def test_role_keyboard_admin() -> None:
    buttons = role_keyboard("admin")
    assert buttons == [
        "Сегодня",
        "Группы",
        "Люди",
        "Прайс",
        "Напоминания",
        "Сообщение",
    ]


def test_role_keyboard_client() -> None:
    buttons = role_keyboard("client")
    assert buttons == [
        "Прайс",
        "Моя группа",
        "Не со своей группой",
        "Остаток",
    ]


def test_reminder_inline_markup_go_and_nogo() -> None:
    sid = "sess-1"
    for kind in ("session_start", "guest_day_of"):
        markup = reminder_inline_markup(
            DueReminder(
                reminder_id="r1",
                person_id="p1",
                telegram_user_id=1,
                person_role="client",
                kind=kind,
                text="text",
                school_session_id=sid,
            )
        )
        assert markup is not None
        row = markup.inline_keyboard[0]
        assert len(row) == 2
        assert row[0].text == "Иду"
        assert row[0].callback_data == f"go:{sid}"
        assert row[1].text == "Не иду"
        assert row[1].callback_data == f"nogo:{sid}"


def test_role_keyboard_guest() -> None:
    buttons = role_keyboard("guest")
    assert buttons == ["Расписание", "Контакты"]


def test_reminder_reply_keyboard_only_for_client_session_start() -> None:
    base = {
        "reminder_id": "r1",
        "person_id": "p1",
        "telegram_user_id": 1,
        "text": "text",
        "school_session_id": "s1",
    }
    client_item = DueReminder(person_role="client", kind="session_start", **base)
    guest_item = DueReminder(person_role="guest", kind="guest_day_of", **base)
    assert (
        reminder_reply_keyboard(client_item).model_dump()
        == reply_markup_for_role("client").model_dump()
    )
    assert reminder_reply_keyboard(guest_item) is None


async def test_send_due_reminder_attaches_client_menu_after_inline() -> None:
    bot = AsyncMock()
    item = DueReminder(
        reminder_id="r1",
        person_id="p1",
        telegram_user_id=42,
        person_role="client",
        kind="session_start",
        text="Занятие сегодня",
        school_session_id="sess-1",
    )
    await send_due_reminder(bot, item)
    assert bot.send_message.await_count == 2
    first = bot.send_message.await_args_list[0]
    second = bot.send_message.await_args_list[1]
    assert first.args[0] == 42
    assert first.kwargs["reply_markup"].inline_keyboard == reminder_inline_markup(
        item
    ).inline_keyboard
    assert second.kwargs["reply_markup"] == reply_markup_for_role("client")


async def test_webhook_secret_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:AAH-fake-token-for-tests")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    get_settings.cache_clear()

    from app.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            missing = await client.post("/telegram/webhook", json={"update_id": 1})
            assert missing.status_code == 401

            ok = await client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
            )
            # Update may fail validation but auth passes
            assert ok.status_code != 401

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
