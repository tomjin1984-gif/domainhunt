from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    dynadot_api_key: str = Field(default="", alias="DYNADOT_API_KEY")
    dynadot_api_base_url: str = Field(
        default="https://api.dynadot.com/api3.json",
        alias="DYNADOT_API_BASE_URL",
    )
    dynadot_sandbox_api_base_url: str = Field(
        default="https://api-sandbox.dynadot.com/api3.json",
        alias="DYNADOT_SANDBOX_API_BASE_URL",
    )
    dynadot_test_mode: bool = Field(default=True, alias="DYNADOT_TEST_MODE")
    dynadot_currency: str = Field(default="USD", alias="DYNADOT_CURRENCY")
    dynadot_timeout_seconds: float = Field(default=10.0, alias="DYNADOT_TIMEOUT_SECONDS")
    dynadot_rate_limit_rpm: int = Field(default=60, alias="DYNADOT_RATE_LIMIT_RPM")

    default_timezone: str = Field(default="Asia/Singapore", alias="DEFAULT_TIMEZONE")
    default_interval_seconds: int = Field(default=5, alias="DEFAULT_INTERVAL_SECONDS")
    default_duration_seconds: int = Field(default=1200, alias="DEFAULT_DURATION_SECONDS")
    default_registration_years: int = Field(default=1, alias="DEFAULT_REGISTRATION_YEARS")

    database_url: str = Field(default="sqlite+aiosqlite:///domainbot.db", alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: Path = Field(default=Path("logs/domainbot.log"), alias="LOG_FILE")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    max_check_history_per_domain: int = Field(default=1000, alias="MAX_CHECK_HISTORY_PER_DOMAIN")
    register_max_attempts: int = Field(default=3, alias="REGISTER_MAX_ATTEMPTS")

    @field_validator("dynadot_currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("default_interval_seconds", "default_duration_seconds", "default_registration_years")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator("dynadot_rate_limit_rpm")
    @classmethod
    def positive_rate_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("DYNADOT_RATE_LIMIT_RPM must be positive")
        return value

    @computed_field
    @property
    def dynadot_effective_base_url(self) -> str:
        if self.dynadot_test_mode:
            return self.dynadot_sandbox_api_base_url
        return self.dynadot_api_base_url

    @computed_field
    @property
    def dynadot_min_safe_interval_seconds(self) -> float:
        return max(1.0, 60.0 / float(self.dynadot_rate_limit_rpm))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
