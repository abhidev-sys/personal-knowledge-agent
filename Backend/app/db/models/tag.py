"""Per-user tags and the many-to-many link to sources."""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_tags_owner_id_name"),
        UniqueConstraint("id", "owner_id", name="uq_tags_id_owner_id"),
        CheckConstraint("name = lower(name)", name="name_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )


class SourceTag(Base):
    __tablename__ = "source_tags"
    __table_args__ = (
        # Both ends must belong to the same owner as this row.
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_source_tags_source_id_sources",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tag_id", "owner_id"],
            ["tags.id", "tags.owner_id"],
            name="fk_source_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        Index("ix_source_tags_tag_id", "tag_id"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)