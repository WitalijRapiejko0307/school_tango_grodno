import pytest
from httpx import ASGITransport, AsyncClient

from app.channels.telegram import role_keyboard
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


def test_role_keyboard_guest() -> None:
    buttons = role_keyboard("guest")
    assert buttons == ["Расписание", "Контакты"]


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
