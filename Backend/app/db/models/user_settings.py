"""One settings row per user: model/provider choice, privacy, memory, retention, digest."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import MemoryMode
from app.db.types import UTCDateTime, enum_column, utcnow


class UserSettings(Base):
    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            "conversation_retention_days IS NULL OR conversation_retention_days > 0",
            name="retention_positive",
        ),
        CheckConstraint("digest_period_days BETWEEN 1 AND 90", name="digest_period_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # Model selection. A NULL model means "use the provider's configured default".
    llm_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ollama", server_default="ollama"
    )
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ollama", server_default="ollama"
    )
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Privacy: nothing leaves the machine/deployment until the user opts in.
    allow_external_processing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # Memory and conversation retention.
    memory_mode: Mapped[MemoryMode] = mapped_column(
        enum_column(MemoryMode, "memory_mode"),
        nullable=False,
        default=MemoryMode.SUGGEST,
        server_default="suggest",
    )
    persist_conversations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    conversation_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Weekly digest.
    weekly_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    digest_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )