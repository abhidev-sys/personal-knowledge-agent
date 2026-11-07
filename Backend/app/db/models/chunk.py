"""A searchable slice of a source, with its embedding and traceability metadata.

The PostgreSQL-only `search_vector` (tsvector) column and the GIN / HNSW indexes are created in
migration 0002 and intentionally NOT mapped here.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
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
from app.db.models.source import Source
from app.db.types import EmbeddingVector, UTCDateTime, utcnow


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # (source_id, owner_id) must match a real source -> a chunk can never belong to the
        # wrong user, and deleting the source deletes its chunks.
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_document_chunks_source_id_sources",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "owner_id", name="uq_document_chunks_id_owner_id"),
        UniqueConstraint("source_id", "sequence", name="uq_document_chunks_source_id_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
        CheckConstraint("char_start IS NULL OR char_start >= 0", name="char_start_non_negative"),
        CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end >= char_start",
            name="char_range_valid",
        ),
        CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        # Never keep a vector without knowing which model produced it (no silent mixing).
        CheckConstraint(
            "embedding IS NULL OR embedding_model IS NOT NULL", name="embedding_requires_model"
        ),
        Index("ix_document_chunks_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )

    source: Mapped[Source] = relationship(back_populates="chunks")