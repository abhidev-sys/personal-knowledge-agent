"""Long-term memory: the record, its audit trail, and the evidence behind it."""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    EvidenceType,
    MemoryAction,
    MemoryActor,
    MemoryStatus,
    MemoryType,
    MemoryVerification,
)
from app.db.types import UTCDateTime, enum_column, utcnow


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_memories_id_owner_id"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from",
            name="validity_order",
        ),
        CheckConstraint("superseded_by_id IS NULL OR superseded_by_id <> id", name="no_self_supersede"),
        CheckConstraint("length(trim(statement)) > 0", name="statement_not_blank"),
        Index("ix_memories_owner_id_status", "owner_id", "status"),
        Index("ix_memories_owner_id_memory_type", "owner_id", "memory_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    memory_type: Mapped[MemoryType] = mapped_column(
        enum_column(MemoryType, "memory_type"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    structured_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. "technical topics"

    status: Mapped[MemoryStatus] = mapped_column(
        enum_column(MemoryStatus, "memory_status"),
        nullable=False,
        default=MemoryStatus.ACTIVE,
        server_default="active",
    )
    verification: Mapped[MemoryVerification] = mapped_column(
        enum_column(MemoryVerification, "memory_verification"),
        nullable=False,
        default=MemoryVerification.USER_STATED,
        server_default="user_stated",
    )
    created_by: Mapped[MemoryActor] = mapped_column(
        enum_column(MemoryActor, "memory_created_by"),
        nullable=False,
        default=MemoryActor.USER,
        server_default="user",
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Single-column FK (SET NULL when the successor is deleted). Same-owner is checked in the
    # service layer, because a composite FK cannot SET NULL only one of its columns.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("memories.id", ondelete="SET NULL", name="fk_memories_superseded_by_id_memories"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class MemoryHistory(Base):
    """Append-only audit trail. Deleting a memory deletes its history (privacy first)."""

    __tablename__ = "memory_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "owner_id"],
            ["memories.id", "memories.owner_id"],
            name="fk_memory_history_memory_id_memories",
            ondelete="CASCADE",
        ),
        UniqueConstraint("memory_id", "version", name="uq_memory_history_memory_id_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_memory_history_memory_id", "memory_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[MemoryAction] = mapped_column(
        enum_column(MemoryAction, "memory_action"), nullable=False
    )
    statement_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[MemoryActor] = mapped_column(
        enum_column(MemoryActor, "memory_changed_by"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )


class MemoryEvidence(Base):
    """Where a memory came from. Rows pointing at a deleted source/chunk are deleted with it."""

    __tablename__ = "memory_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "owner_id"],
            ["memories.id", "memories.owner_id"],
            name="fk_memory_evidence_memory_id_memories",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_memory_evidence_source_id_sources",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "owner_id"],
            ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_memory_evidence_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "evidence_type = 'user_statement' OR source_id IS NOT NULL",
            name="source_required_unless_statement",
        ),
        CheckConstraint("chunk_id IS NULL OR source_id IS NOT NULL", name="chunk_requires_source"),
        Index("ix_memory_evidence_memory_id", "memory_id"),
        Index("ix_memory_evidence_source_id", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    evidence_type: Mapped[EvidenceType] = mapped_column(
        enum_column(EvidenceType, "evidence_type"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )