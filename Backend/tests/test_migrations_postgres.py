"""Runs the real migrations against a THROWAWAY PostgreSQL database (pgvector required).

    $env:PKA_TEST_POSTGRES_URL = "postgresql+psycopg://user:pass@host:5432/scratch_db"
    python -m pytest tests/test_migrations_postgres.py -v

WARNING: the test runs `alembic downgrade base`, which drops all PKA tables in that database.
"""
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.types import EMBEDDING_DIM

PG_URL = os.environ.get("PKA_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PKA_TEST_POSTGRES_URL to run")

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def pg(monkeypatch):
    assert PG_URL
    monkeypatch.setenv("PKA_DATABASE_URL", PG_URL)
    monkeypatch.setenv("PKA_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PKA_ENVIRONMENT", "test")
    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    engine = create_engine(PG_URL)
    command.downgrade(cfg, "base")  # clean slate
    yield cfg, engine
    command.downgrade(cfg, "base")
    engine.dispose()
    get_settings.cache_clear()


def test_upgrade_creates_schema_matching_models(pg):
    cfg, engine = pg
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        insp = inspect(conn)
        tables = set(insp.get_table_names())
        chunk_cols = {c["name"] for c in insp.get_columns("document_chunks")}
    assert {
        "users", "user_settings", "tags", "sources", "source_tags", "document_chunks", "jobs"
    } <= tables
    assert {"embedding", "search_vector"} <= chunk_cols
    command.check(cfg)  # raises if the models and the migrated database differ


def test_fulltext_vector_and_cascade_work_in_postgres(pg):
    cfg, engine = pg
    command.upgrade(cfg, "head")
    uid, sid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vec = "[" + ",".join(["0.1"] * EMBEDDING_DIM) + "]"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'a@example.com')"), {"id": uid}
        )
        conn.execute(
            text(
                "INSERT INTO sources (id, owner_id, source_type, title) "
                "VALUES (:id, :o, 'note', 'n')"
            ),
            {"id": sid, "o": uid},
        )
        conn.execute(
            text(
                "INSERT INTO document_chunks (id, source_id, owner_id, sequence, content, "
                "content_hash, embedding_model, embedding) VALUES "
                "(:id, :s, :o, 0, 'Retrieval augmented generation', :h, 'm', CAST(:v AS vector))"
            ),
            {"id": cid, "s": sid, "o": uid, "h": "a" * 64, "v": vec},
        )
    with engine.connect() as conn:
        fts = conn.scalar(
            text(
                "SELECT count(*) FROM document_chunks "
                "WHERE search_vector @@ plainto_tsquery('english', 'retrieval')"
            )
        )
        nearest = conn.scalar(
            text("SELECT id FROM document_chunks ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"),
            {"v": vec},
        )
    assert fts == 1
    assert nearest == cid

    with pytest.raises(DBAPIError), engine.begin() as conn:  # wrong dimension is refused
        conn.execute(text("UPDATE document_chunks SET embedding = CAST('[0.1,0.2]' AS vector)"))

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sources WHERE id = :id"), {"id": sid})
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM document_chunks")) == 0