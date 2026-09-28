from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "sqlite+aiosqlite:///./tango.db"
    BOT_TOKEN: str = ""
    WEBHOOK_SECRET: str = ""
    SCHOOL_TZ: str = "Europe/Minsk"
    PUBLIC_URL: str = ""
    BOOTSTRAP_ADMIN_TELEGRAM_ID: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
