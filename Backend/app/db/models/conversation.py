"""Chat history. Only stored when the user's retention settings allow it."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import AnswerStatus, MessageRole
from app.db.types import UTCDateTime, enum_column, utcnow


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_conversations_id_owner_id"),
        Index("ix_conversations_owner_id_updated_at", "owner_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.owner_id"],
            name="fk_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "owner_id", name="uq_messages_id_owner_id"),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0", name="completion_tokens_non_negative"
        ),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[MessageRole] = mapped_column(enum_column(MessageRole, "message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    answer_status: Mapped[AnswerStatus | None] = mapped_column(
        enum_column(AnswerStatus, "answer_status"), nullable=True
    )
    # Set by the service BEFORE a cited source is deleted: the text may still paraphrase it.
    sources_removed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )


class MessageCitation(Base):
    """A citation shown with an assistant answer. Deleted together with its source."""

    __tablename__ = "message_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "owner_id"],
            ["messages.id", "messages.owner_id"],
            name="fk_message_citations_message_id_messages",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_message_citations_source_id_sources",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "owner_id"],
            ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_message_citations_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "message_id", "citation_index", name="uq_message_citations_message_id_citation_index"
        ),
        CheckConstraint("citation_index >= 1", name="citation_index_positive"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
        Index("ix_message_citations_source_id", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    citation_index: Mapped[int] = mapped_column(Integer, nullable=False)  # the [1], [2] marker
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_title: Mapped[str] = mapped_column(String(500), nullable=False)  # snapshot for display
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)


class MessageMemory(Base):
    """Which memories were used to produce an answer."""

    __tablename__ = "message_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "owner_id"],
            ["messages.id", "messages.owner_id"],
            name="fk_message_memories_message_id_messages",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["memory_id", "owner_id"],
            ["memories.id", "memories.owner_id"],
            name="fk_message_memories_memory_id_memories",
            ondelete="CASCADE",
        ),
        Index("ix_message_memories_memory_id", "memory_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)