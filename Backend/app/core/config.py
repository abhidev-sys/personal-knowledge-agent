"""Central, validated configuration. The app refuses to start on bad config."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_prefix="PKA_",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str
    secret_key: str = Field(min_length=32)
    allowed_origins: list[str] = Field(default_factory=list)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _production_rules(self) -> "Settings":
        if self.environment == "production":
            if "*" in self.allowed_origins:
                raise ValueError("PKA_ALLOWED_ORIGINS must not contain '*' in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("SQLite is not allowed in production; use PostgreSQL")
        return self


@lru_cache
def get_settings() -> Settings:
    # Raises pydantic.ValidationError with a clear message if config is missing/invalid.
    return Settings()  # type: ignore[call-arg]