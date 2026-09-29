from app.db import prepare_async_database_url


def test_sqlite_url_unchanged() -> None:
    url = "sqlite+aiosqlite:///:memory:"
    normalized, connect_args = prepare_async_database_url(url)
    assert normalized == url
    assert connect_args == {}


def test_postgres_strips_sslmode_and_enables_ssl() -> None:
    url = "postgresql://user:pass@host:5432/db?sslmode=require&channel_binding=prefer"
    normalized, connect_args = prepare_async_database_url(url)
    assert normalized == "postgresql+asyncpg://user:pass@host:5432/db"
    assert connect_args == {"ssl": True}


def test_postgres_without_sslmode() -> None:
    url = "postgres://user:pass@localhost:5432/db"
    normalized, connect_args = prepare_async_database_url(url)
    assert normalized == "postgresql+asyncpg://user:pass@localhost:5432/db"
    assert connect_args == {}
