"""A piece of saved knowledge: note, PDF, bookmark, Markdown or text file."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import IngestionStatus, SourceType
from app.db.types import UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.db.models.chunk import DocumentChunk


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        # Target for composite foreign keys: lets child tables prove "same owner as the source".
        UniqueConstraint("id", "owner_id", name="uq_sources_id_owner_id"),
        # Idempotent ingestion: identical content cannot be stored twice per user and type.
        UniqueConstraint(
            "owner_id",
            "source_type",
            "content_hash",
            name="uq_sources_owner_id_source_type_content_hash",
        ),
        # One bookmark per canonical URL per user (NULLs are distinct, so notes are unaffected).
        UniqueConstraint("owner_id", "canonical_url", name="uq_sources_owner_id_canonical_url"),
        CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64", name="content_hash_length"
        ),
        CheckConstraint(
            "source_type <> 'bookmark' OR original_url IS NOT NULL", name="bookmark_requires_url"
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_non_negative"),
        Index("ix_sources_owner_id_status", "owner_id", "status"),
        Index("ix_sources_owner_id_created_at", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[SourceType] = mapped_column(
        enum_column(SourceType, "source_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)

    original_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Normalized extracted text (source of truth for reindexing) and its SHA-256 hex digest.
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[IngestionStatus] = mapped_column(
        enum_column(IngestionStatus, "ingestion_status"),
        nullable=False,
        default=IngestionStatus.PENDING,
        server_default="pending",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    # passive_deletes: the database cascades the delete; the ORM does not load every chunk first.
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.sequence",
    )