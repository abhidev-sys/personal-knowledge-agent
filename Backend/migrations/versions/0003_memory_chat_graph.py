"""memory, conversations, citations, knowledge graph

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _in(column: str, values: list[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


MEMORY_TYPES = ["preference", "goal", "project", "fact", "instruction", "correction",
                "temporary", "hypothesis"]
MEMORY_STATUSES = ["active", "candidate", "superseded", "rejected", "archived"]
VERIFICATIONS = ["user_confirmed", "user_stated", "system_inferred"]
ACTORS = ["user", "system"]
ACTIONS = ["created", "updated", "confirmed", "superseded", "rejected", "archived", "restored"]
EVIDENCE_TYPES = ["source", "chunk", "user_statement"]
ROLES = ["user", "assistant"]
ANSWER_STATUSES = ["answered", "insufficient_evidence", "error"]
NODE_TYPES = ["source", "memory", "concept"]
RELATION_TYPES = ["related_to", "supports", "contradicts", "extends", "references", "supersedes"]
RELATION_ORIGINS = ["textual_reference", "semantic_similarity", "user_created", "llm_suggested"]
RELATION_STATUSES = ["candidate", "confirmed", "dismissed"]


def upgrade() -> None:
    # ---- memories ------------------------------------------------------------------------
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("structured_value", sa.JSON(), nullable=True),
        sa.Column("scope", sa.String(200), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("verification", sa.String(32), nullable=False, server_default="user_stated"),
        sa.Column("created_by", sa.String(32), nullable=False, server_default="user"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_memories"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_memories_owner_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"], ["memories.id"],
            name="fk_memories_superseded_by_id_memories", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_memories_id_owner_id"),
        sa.CheckConstraint(_in("memory_type", MEMORY_TYPES), name=op.f("ck_memories_memory_type")),
        sa.CheckConstraint(_in("status", MEMORY_STATUSES), name=op.f("ck_memories_memory_status")),
        sa.CheckConstraint(
            _in("verification", VERIFICATIONS), name=op.f("ck_memories_memory_verification")
        ),
        sa.CheckConstraint(_in("created_by", ACTORS), name=op.f("ck_memories_memory_created_by")),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_memories_confidence_range"),
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from",
            name=op.f("ck_memories_validity_order"),
        ),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name=op.f("ck_memories_no_self_supersede"),
        ),
        sa.CheckConstraint("length(trim(statement)) > 0", name=op.f("ck_memories_statement_not_blank")),
    )
    op.create_index("ix_memories_owner_id_status", "memories", ["owner_id", "status"])
    op.create_index("ix_memories_owner_id_memory_type", "memories", ["owner_id", "memory_type"])

    # ---- memory_history ------------------------------------------------------------------
    op.create_table(
        "memory_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("statement_before", sa.Text(), nullable=True),
        sa.Column("statement_after", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_history"),
        sa.ForeignKeyConstraint(
            ["memory_id", "owner_id"], ["memories.id", "memories.owner_id"],
            name="fk_memory_history_memory_id_memories", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("memory_id", "version", name="uq_memory_history_memory_id_version"),
        sa.CheckConstraint("version >= 1", name=op.f("ck_memory_history_version_positive")),
        sa.CheckConstraint(_in("action", ACTIONS), name=op.f("ck_memory_history_memory_action")),
        sa.CheckConstraint(
            _in("changed_by", ACTORS), name=op.f("ck_memory_history_memory_changed_by")
        ),
    )
    op.create_index("ix_memory_history_memory_id", "memory_history", ["memory_id"])

    # ---- memory_evidence -----------------------------------------------------------------
    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_evidence"),
        sa.ForeignKeyConstraint(
            ["memory_id", "owner_id"], ["memories.id", "memories.owner_id"],
            name="fk_memory_evidence_memory_id_memories", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"], ["sources.id", "sources.owner_id"],
            name="fk_memory_evidence_source_id_sources", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "owner_id"], ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_memory_evidence_chunk_id_document_chunks", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _in("evidence_type", EVIDENCE_TYPES), name=op.f("ck_memory_evidence_evidence_type")
        ),
        sa.CheckConstraint(
            "evidence_type = 'user_statement' OR source_id IS NOT NULL",
            name=op.f("ck_memory_evidence_source_required_unless_statement"),
        ),
        sa.CheckConstraint(
            "chunk_id IS NULL OR source_id IS NOT NULL",
            name=op.f("ck_memory_evidence_chunk_requires_source"),
        ),
    )
    op.create_index("ix_memory_evidence_memory_id", "memory_evidence", ["memory_id"])
    op.create_index("ix_memory_evidence_source_id", "memory_evidence", ["source_id"])

    # ---- conversations -------------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_conversations_owner_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_conversations_id_owner_id"),
    )
    op.create_index(
        "ix_conversations_owner_id_updated_at", "conversations", ["owner_id", "updated_at"]
    )

    # ---- messages ------------------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer_status", sa.String(32), nullable=True),
        sa.Column("sources_removed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "owner_id"], ["conversations.id", "conversations.owner_id"],
            name="fk_messages_conversation_id_conversations", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_messages_id_owner_id"),
        sa.CheckConstraint(_in("role", ROLES), name=op.f("ck_messages_message_role")),
        sa.CheckConstraint(
            _in("answer_status", ANSWER_STATUSES), name=op.f("ck_messages_answer_status")
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name=op.f("ck_messages_prompt_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name=op.f("ck_messages_completion_tokens_non_negative"),
        ),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"]
    )

    # ---- message_citations ---------------------------------------------------------------
    op.create_table(
        "message_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("citation_index", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("source_title", sa.String(500), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_heading", sa.String(500), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_message_citations"),
        sa.ForeignKeyConstraint(
            ["message_id", "owner_id"], ["messages.id", "messages.owner_id"],
            name="fk_message_citations_message_id_messages", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"], ["sources.id", "sources.owner_id"],
            name="fk_message_citations_source_id_sources", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "owner_id"], ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_message_citations_chunk_id_document_chunks", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "message_id", "citation_index", name="uq_message_citations_message_id_citation_index"
        ),
        sa.CheckConstraint(
            "citation_index >= 1", name=op.f("ck_message_citations_citation_index_positive")
        ),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name=op.f("ck_message_citations_page_number_positive"),
        ),
    )
    op.create_index("ix_message_citations_source_id", "message_citations", ["source_id"])

    # ---- message_memories ----------------------------------------------------------------
    op.create_table(
        "message_memories",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("message_id", "memory_id", name="pk_message_memories"),
        sa.ForeignKeyConstraint(
            ["message_id", "owner_id"], ["messages.id", "messages.owner_id"],
            name="fk_message_memories_message_id_messages", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id", "owner_id"], ["memories.id", "memories.owner_id"],
            name="fk_message_memories_memory_id_memories", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_message_memories_memory_id", "message_memories", ["memory_id"])

    # ---- knowledge_nodes -----------------------------------------------------------------
    op.create_table(
        "knowledge_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("node_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.String(200), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_nodes"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_knowledge_nodes_owner_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"], ["sources.id", "sources.owner_id"],
            name="fk_knowledge_nodes_source_id_sources", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id", "owner_id"], ["memories.id", "memories.owner_id"],
            name="fk_knowledge_nodes_memory_id_memories", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_knowledge_nodes_id_owner_id"),
        sa.UniqueConstraint("owner_id", "source_id", name="uq_knowledge_nodes_owner_id_source_id"),
        sa.UniqueConstraint("owner_id", "memory_id", name="uq_knowledge_nodes_owner_id_memory_id"),
        sa.UniqueConstraint("owner_id", "label", name="uq_knowledge_nodes_owner_id_label"),
        sa.CheckConstraint(_in("node_type", NODE_TYPES), name=op.f("ck_knowledge_nodes_node_type")),
        sa.CheckConstraint(
            "(node_type = 'source' AND source_id IS NOT NULL AND memory_id IS NULL AND label IS NULL)"
            " OR (node_type = 'memory' AND memory_id IS NOT NULL AND source_id IS NULL AND label IS NULL)"
            " OR (node_type = 'concept' AND label IS NOT NULL AND source_id IS NULL AND memory_id IS NULL)",
            name=op.f("ck_knowledge_nodes_type_matches_reference"),
        ),
    )

    # ---- knowledge_relations -------------------------------------------------------------
    op.create_table(
        "knowledge_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("from_node_id", sa.Uuid(), nullable=False),
        sa.Column("to_node_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_relations"),
        sa.ForeignKeyConstraint(
            ["from_node_id", "owner_id"], ["knowledge_nodes.id", "knowledge_nodes.owner_id"],
            name="fk_knowledge_relations_from_node_id_knowledge_nodes", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_node_id", "owner_id"], ["knowledge_nodes.id", "knowledge_nodes.owner_id"],
            name="fk_knowledge_relations_to_node_id_knowledge_nodes", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_knowledge_relations_id_owner_id"),
        sa.UniqueConstraint(
            "owner_id", "from_node_id", "to_node_id", "relation_type", name="uq_relations_edge"
        ),
        sa.CheckConstraint(
            _in("relation_type", RELATION_TYPES), name=op.f("ck_knowledge_relations_relation_type")
        ),
        sa.CheckConstraint(
            _in("origin", RELATION_ORIGINS), name=op.f("ck_knowledge_relations_relation_origin")
        ),
        sa.CheckConstraint(
            _in("status", RELATION_STATUSES), name=op.f("ck_knowledge_relations_relation_status")
        ),
        sa.CheckConstraint("from_node_id <> to_node_id", name=op.f("ck_knowledge_relations_no_self_loop")),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name=op.f("ck_knowledge_relations_score_range"),
        ),
        sa.CheckConstraint(
            "origin <> 'semantic_similarity' OR relation_type = 'related_to'",
            name=op.f("ck_knowledge_relations_similarity_only_related"),
        ),
    )
    op.create_index("ix_knowledge_relations_from_node_id", "knowledge_relations", ["from_node_id"])
    op.create_index("ix_knowledge_relations_to_node_id", "knowledge_relations", ["to_node_id"])
    op.create_index(
        "ix_knowledge_relations_owner_id_status", "knowledge_relations", ["owner_id", "status"]
    )

    # ---- relation_evidence ---------------------------------------------------------------
    op.create_table(
        "relation_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("relation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_relation_evidence"),
        sa.ForeignKeyConstraint(
            ["relation_id", "owner_id"], ["knowledge_relations.id", "knowledge_relations.owner_id"],
            name="fk_relation_evidence_relation_id_knowledge_relations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "owner_id"], ["document_chunks.id", "document_chunks.owner_id"],
            name="fk_relation_evidence_chunk_id_document_chunks", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_relation_evidence_relation_id", "relation_evidence", ["relation_id"])


def downgrade() -> None:
    for table in (
        "relation_evidence",
        "knowledge_relations",
        "knowledge_nodes",
        "message_memories",
        "message_citations",
        "messages",
        "conversations",
        "memory_evidence",
        "memory_history",
        "memories",
    ):
        op.drop_table(table)