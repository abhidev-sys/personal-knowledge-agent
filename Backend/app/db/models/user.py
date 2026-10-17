"""Minimal users table so the first migration is meaningful. Expanded in Phase 4."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

UTC = timezone.utc


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )