"""core schema: user columns, settings, tags, sources, chunks, jobs

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 768  # must equal app.db.types.EMBEDDING_DIM (a test enforces this)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Migration 0002 requires PostgreSQL (pgvector + tsvector).")

    # ---- users: extend the Phase 1 stub -------------------------------------------------
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(120), nullable=True))
    op.add_column(
        "users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("users", _ts("updated_at"))
    op.alter_column("users", "created_at", server_default=sa.func.now())
    op.create_check_constraint(op.f("ck_users_email_lowercase"), "users", "email = lower(email)")

    # ---- user_settings ------------------------------------------------------------------
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("llm_provider", sa.String(32), nullable=False, server_default="ollama"),
        sa.Column("llm_model", sa.String(100), nullable=True),
        sa.Column("embedding_provider", sa.String(32), nullable=False, server_default="ollama"),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column(
            "allow_external_processing", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("memory_mode", sa.String(32), nullable=False, server_default="suggest"),
        sa.Column("persist_conversations", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("conversation_retention_days", sa.Integer(), nullable=True),
        sa.Column("weekly_digest_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("digest_period_days", sa.Integer(), nullable=False, server_default="7"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_settings"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_settings_user_id_users", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "memory_mode IN ('off', 'suggest', 'auto')", name=op.f("ck_user_settings_memory_mode")
        ),
        sa.CheckConstraint(
            "conversation_retention_days IS NULL OR conversation_retention_days > 0",
            name=op.f("ck_user_settings_retention_positive"),
        ),
        sa.CheckConstraint(
            "digest_period_days BETWEEN 1 AND 90",
            name=op.f("ck_user_settings_digest_period_range"),
        ),
    )

    # ---- tags ---------------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_tags_owner_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("owner_id", "name", name="uq_tags_owner_id_name"),
        sa.UniqueConstraint("id", "owner_id", name="uq_tags_id_owner_id"),
        sa.CheckConstraint("name = lower(name)", name=op.f("ck_tags_name_lowercase")),
    )

    # ---- sources ------------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_url", sa.String(2048), nullable=True),
        sa.Column("canonical_url", sa.String(2048), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("storage_path", sa.String(1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_sources_owner_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_sources_id_owner_id"),
        sa.UniqueConstraint(
            "owner_id",
            "source_type",
            "content_hash",
            name="uq_sources_owner_id_source_type_content_hash",
        ),
        sa.UniqueConstraint("owner_id", "canonical_url", name="uq_sources_owner_id_canonical_url"),
        sa.CheckConstraint(
            "source_type IN ('note', 'pdf', 'bookmark', 'markdown', 'text')",
            name=op.f("ck_sources_source_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name=op.f("ck_sources_ingestion_status"),
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name=op.f("ck_sources_content_hash_length"),
        ),
        sa.CheckConstraint(
            "source_type <> 'bookmark' OR original_url IS NOT NULL",
            name=op.f("ck_sources_bookmark_requires_url"),
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name=op.f("ck_sources_size_non_negative")
        ),
    )
    op.create_index("ix_sources_owner_id_status", "sources", ["owner_id", "status"])
    op.create_index("ix_sources_owner_id_created_at", "sources", ["owner_id", "created_at"])

    # ---- source_tags --------------------------------------------------------------------
    op.create_table(
        "source_tags",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("source_id", "tag_id", name="pk_source_tags"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_source_tags_source_id_sources",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id", "owner_id"],
            ["tags.id", "tags.owner_id"],
            name="fk_source_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_source_tags_tag_id", "source_tags", ["tag_id"])

    # ---- document_chunks ----------------------------------------------------------------
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_heading", sa.String(500), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_document_chunks_source_id_sources",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_document_chunks_id_owner_id"),
        sa.UniqueConstraint("source_id", "sequence", name="uq_document_chunks_source_id_sequence"),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_document_chunks_sequence_non_negative")),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name=op.f("ck_document_chunks_page_number_positive"),
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_start >= 0",
            name=op.f("ck_document_chunks_char_start_non_negative"),
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end >= char_start",
            name=op.f("ck_document_chunks_char_range_valid"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_document_chunks_content_hash_length")
        ),
    )
    op.create_index("ix_document_chunks_owner_id", "document_chunks", ["owner_id"])

    # PostgreSQL-only columns and indexes (raw SQL; ignored by Alembic's autogenerate check).
    op.execute(f"ALTER TABLE document_chunks ADD COLUMN embedding vector({EMBEDDING_DIM})")
    op.create_check_constraint(
        op.f("ck_document_chunks_embedding_requires_model"),
        "document_chunks",
        "embedding IS NULL OR embedding_model IS NOT NULL",
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX ix_pg_document_chunks_search_vector "
        "ON document_chunks USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_pg_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    # ---- jobs ---------------------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_jobs_owner_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["sources.id", "sources.owner_id"],
            name="fk_jobs_source_id_sources",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name=op.f("ck_jobs_job_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
        sa.CheckConstraint("attempts <= max_attempts", name=op.f("ck_jobs_attempts_within_limit")),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("document_chunks")  # also drops its indexes and PostgreSQL-only columns
    op.drop_table("source_tags")
    op.drop_table("sources")
    op.drop_table("tags")
    op.drop_table("user_settings")
    op.drop_constraint(op.f("ck_users_email_lowercase"), "users", type_="check")
    op.alter_column("users", "created_at", server_default=None)
    op.drop_column("users", "updated_at")
    op.drop_column("users", "is_active")
    op.drop_column("users", "display_name")
    op.drop_column("users", "password_hash")