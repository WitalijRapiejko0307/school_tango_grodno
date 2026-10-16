import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db import Base, prepare_async_database_url
from app import models as _models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> tuple[str, dict]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = "sqlite+aiosqlite:///./tango.db"
    return prepare_async_database_url(url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url, _connect_args = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url, connect_args = get_database_url()
    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async def run() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
        await connectable.dispose()

    asyncio.run(run())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
