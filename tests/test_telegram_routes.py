import pytest
from httpx import ASGITransport, AsyncClient

from unittest.mock import AsyncMock

from app.channels.telegram import (
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
    send_due_reminder,
)
from app.services.notifications import DueReminder
from app.config import get_settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
        "Гости",
        "Админы",
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
