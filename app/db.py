from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

_ASYNCPG_STRIP_QUERY_KEYS = frozenset({"sslmode", "channel_binding"})
_SSLMODE_REQUIRES_SSL = frozenset({"require", "verify-ca", "verify-full", "prefer"})


def prepare_async_database_url(database_url: str) -> tuple[str, dict]:
    """Normalize DATABASE_URL for SQLAlchemy async engines (asyncpg / aiosqlite)."""
    parsed = urlparse(database_url)
    scheme = parsed.scheme.split("+", 1)[0]

    if scheme == "sqlite":
        return database_url, {}

    if scheme not in ("postgres", "postgresql"):
        return database_url, {}

    query = parse_qs(parsed.query, keep_blank_values=True)
    needs_ssl = False
    filtered: list[tuple[str, str]] = []
    for key, values in query.items():
        key_lower = key.lower()
        if key_lower in _ASYNCPG_STRIP_QUERY_KEYS:
            if key_lower == "sslmode" and values:
                mode = values[0].lower()
                if mode in _SSLMODE_REQUIRES_SSL:
                    needs_ssl = True
            continue
        for value in values:
            filtered.append((key, value))

    driver_scheme = parsed.scheme
    if driver_scheme in ("postgres", "postgresql"):
        driver_scheme = "postgresql+asyncpg"

    normalized = urlunparse(
        parsed._replace(
            scheme=driver_scheme,
            query=urlencode(filtered),
        )
    )
    connect_args = {"ssl": True} if needs_ssl else {}
    return normalized, connect_args


class Base(DeclarativeBase):
    pass


settings = get_settings()
_db_url, _connect_args = prepare_async_database_url(settings.DATABASE_URL)
engine = create_async_engine(
    _db_url,
    echo=False,
    connect_args=_connect_args,
)
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


# Register ORM models on Base.metadata (imports after Base/engine setup).
from app import models as _models  # noqa: E402, F401
