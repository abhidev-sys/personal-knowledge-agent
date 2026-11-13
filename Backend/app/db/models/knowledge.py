"""Knowledge graph in plain relational tables: nodes, typed relations, and evidence."""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import NodeType, RelationOrigin, RelationStatus, RelationType
from app.db.types import UTCDateTime, enum_column, utcnow


class KnowledgeNode(Base):
    """A source, a memory, or a free-standing concept. Exactly one reference is set."""

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_knowledge_nodes_source_id_sources",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["memory_id", "owner_id"],
            ["memories.id", "memories.owner_id"],
            name="fk_knowledge_nodes_memory_id_memories",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "owner_id", name="uq_knowledge_nodes_id_owner_id"),
        UniqueConstraint("owner_id", "source_id", name="uq_knowledge_nodes_owner_id_source_id"),
        UniqueConstraint("owner_id", "memory_id", name="uq_knowledge_nodes_owner_id_memory_id"),
        UniqueConstraint("owner_id", "label", name="uq_knowledge_nodes_owner_id_label"),
        CheckConstraint(
            "(node_type = 'source' AND source_id IS NOT NULL AND memory_id IS NULL AND label IS NULL)"
            " OR (node_type = 'memory' AND memory_id IS NOT NULL AND source_id IS NULL AND label IS NULL)"
            " OR (node_type = 'concept' AND label IS NOT NULL AND source_id IS NULL AND memory_id IS NULL)",
            name="type_matches_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    node_type: Mapped[NodeType] = mapped_column(enum_column(NodeType, "node_type"), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)  # concepts only
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["from_node_id", "owner_id"],
            ["knowledge_nodes.id", "knowledge_nodes.owner_id"],
            name="fk_knowledge_relations_from_node_id_knowledge_nodes",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["to_node_id", "owner_id"],
            ["knowledge_nodes.id", "knowledge_nodes.owner_id"],
            name="fk_knowledge_relations_to_node_id_knowledge_nodes",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "owner_id", name="uq_knowledge_relations_id_owner_id"),
        UniqueConstraint(
            "owner_id", "from_node_id", "to_node_id", "relation_type", name="uq_relations_edge"
        ),
        CheckConstraint("from_node_id <> to_node_id", name="no_self_loop"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score_range"),
        # Embedding closeness alone can only say "related", never contradicts/supersedes/etc.
        CheckConstraint(
            "origin <> 'semantic_similarity' OR relation_type = 'related_to'",
            name="similarity_only_related",
        ),
        Index("ix_knowledge_relations_from_node_id", "from_node_id"),
        Index("ix_knowledge_relations_to_node_id", "to_node_id"),
        Index("ix_knowledge_relations_owner_id_status", "owner_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    from_node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    to_node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    relation_type: Mapped[RelationType] = mapped_column(
        enum_column(RelationType, "relation_type"), nullable=False
    )
    origin: Mapped[RelationOrigin] = mapped_column(
        enum_column(RelationOrigin, "relation_origin"), nullable=False
    )
    status: Mapped[RelationStatus] = mapped_column(
        enum_column(RelationStatus, "relation_status"),
        nullable=False,
        default=RelationStatus.CANDIDATE,
        server_default="candidate",
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class RelationEvidence(Base):
    __tablename__ = "relation_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["relation_id", "owner_id"],
            ["knowledge_relations.id", "knowledge_relations.owner_id"],
            name="fk_relation_evidence_relation_id_knowledge_relations",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "owner_id"],
            ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_relation_evidence_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        Index("ix_relation_evidence_relation_id", "relation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    relation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)