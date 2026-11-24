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

    # ---- ingestion limits (Phase 5) -------------------------------------------------
    upload_dir: str = "./data/uploads"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_text_chars: int = Field(default=2_000_000, ge=1000)
    max_pdf_pages: int = Field(default=300, ge=1)
    chunk_size_chars: int = Field(default=1000, ge=200, le=8000)
    chunk_overlap_chars: int = Field(default=150, ge=0)
    embedding_batch_size: int = Field(default=32, ge=1, le=512)

    # ---- URL fetching (Phase 5) -----------------------------------------------------
    fetch_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    fetch_total_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    fetch_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    fetch_max_redirects: int = Field(default=5, ge=0, le=10)

    # ---- background jobs (Phase 5B) -------------------------------------------------
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_retry_base_seconds: float = Field(default=30.0, ge=0, le=3600)
    job_lock_timeout_seconds: float = Field(default=900.0, ge=10)
    job_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)

    # ---- temporary stand-ins (replaced by real auth in Phase 8, real providers in Phase 16)
    dev_auth_enabled: bool = False  # lets every request act as one local user
    dev_user_email: str = "local@example.com"
    embedding_backend: Literal["none", "test-hashing"] = "none"

    @model_validator(mode="after")
    def _validate_rules(self) -> "Settings":
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("PKA_CHUNK_OVERLAP_CHARS must be smaller than PKA_CHUNK_SIZE_CHARS")
        if self.environment == "production":
            if "*" in self.allowed_origins:
                raise ValueError("PKA_ALLOWED_ORIGINS must not contain '*' in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("SQLite is not allowed in production; use PostgreSQL")
            if self.dev_auth_enabled:
                raise ValueError("PKA_DEV_AUTH_ENABLED must be false in production")
            if self.embedding_backend == "test-hashing":
                raise ValueError("The test embedder must not be used in production")
        return self


@lru_cache
def get_settings() -> Settings:
    # Raises pydantic.ValidationError with a clear message if config is missing/invalid.
    return Settings()  # type: ignore[call-arg]