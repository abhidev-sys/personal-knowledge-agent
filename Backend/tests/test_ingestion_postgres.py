"""Ingestion, search primitives and the API against a REAL PostgreSQL (migrated with Alembic).

    $env:PKA_TEST_POSTGRES_URL = "postgresql+psycopg://user:pass@host/db?sslmode=require"
    & $py -m pytest tests/test_ingestion_postgres.py -v

Use a scratch database. These tests create their own users and delete them afterwards.
"""
import os
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.core.config import Settings, get_settings
from app.db.models import DocumentChunk, Job, Source, User
from app.db.session import build_engine, build_sessionmaker
from app.ingestion.storage import FileStorage
from app.jobs.queue import claim_next, enqueue
from app.main import create_app
from app.providers.embeddings import HashingEmbedder
from app.services.ingestion_service import IngestionService

PG_URL = os.environ.get("PKA_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PKA_TEST_POSTGRES_URL to run")

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def pg(monkeypatch, tmp_path):
    assert PG_URL
    monkeypatch.setenv("PKA_DATABASE_URL", PG_URL)
    monkeypatch.setenv("PKA_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PKA_ENVIRONMENT", "test")
    get_settings.cache_clear()
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(config, "head")

    engine = build_engine(PG_URL)
    factory = build_sessionmaker(engine)
    settings = Settings(
        environment="test",
        database_url=PG_URL,
        secret_key="x" * 40,
        upload_dir=str(tmp_path / "uploads"),
        embedding_backend="test-hashing",
    )
    user_ids: list[uuid.UUID] = []

    def new_user() -> uuid.UUID:
        with factory() as session:
            user = User(email=f"pgtest-{uuid.uuid4().hex[:10]}@example.com")
            session.add(user)
            session.commit()
            user_ids.append(user.id)
            return user.id

    def service(session) -> IngestionService:
        return IngestionService(
            session, settings, storage=FileStorage(settings.upload_dir), embedder=HashingEmbedder()
        )

    yield SimpleNamespace(factory=factory, settings=settings, new_user=new_user, service=service)

    with factory() as session:
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()
    engine.dispose()
    get_settings.cache_clear()


def test_full_text_and_vector_search_over_ingested_notes(pg):
    owner = pg.new_user()
    embedder = HashingEmbedder()
    with pg.factory() as session:
        service = pg.service(session)
        service.create_note(owner, "RAG", "Retrieval augmented generation combines search with language models.")
        service.create_note(owner, "Cooking", "Boil the pasta and add tomato sauce with basil.")
    vector = "[" + ",".join(str(v) for v in embedder.embed(["retrieval augmented generation"])[0]) + "]"
    with pg.factory() as session:
        full_text = session.execute(
            text(
                "SELECT content FROM document_chunks WHERE owner_id = :o "
                "AND search_vector @@ plainto_tsquery('english', 'retrieval')"
            ),
            {"o": owner},
        ).scalars().all()
        assert len(full_text) == 1 and "Retrieval" in full_text[0]
        nearest = session.execute(
            text(
                "SELECT content FROM document_chunks WHERE owner_id = :o "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"
            ),
            {"o": owner, "v": vector},
        ).scalar_one()
        assert "Retrieval" in nearest
        stats = session.execute(
            text("SELECT count(*), count(embedding) FROM document_chunks WHERE owner_id = :o"),
            {"o": owner},
        ).one()
        assert tuple(stats) == (2, 2)


def test_two_workers_never_claim_the_same_job(pg):
    owner = pg.new_user()
    job_types = [f"pgtest-{uuid.uuid4().hex[:6]}-{i}" for i in range(12)]
    with pg.factory() as session:
        for job_type in job_types:
            enqueue(session, owner_id=owner, source_id=None, job_type=job_type)
        session.commit()

    claimed: list = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def work(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            while True:
                with pg.factory() as session:
                    job = claim_next(session, worker_id=name)
                if job is None:
                    return
                with lock:
                    claimed.append(job)
        except Exception as exc:  # reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    ours = [job for job in claimed if job.job_type in set(job_types)]
    ids = [job.id for job in ours]
    assert len(ids) == 12 and len(set(ids)) == 12
    assert {job.attempts for job in ours} == {1}


def test_deleting_sources_removes_chunks_and_jobs_on_postgres(pg):
    owner = pg.new_user()
    with pg.factory() as session:
        service = pg.service(session)
        note_id = service.create_note(owner, "N", "alpha beta gamma delta").source.id
        bookmark_id = service.create_bookmark(owner, f"https://example.com/pg-{uuid.uuid4().hex}").source.id
    with pg.factory() as session:
        service = pg.service(session)
        service.delete_source(owner, note_id)
        service.delete_source(owner, bookmark_id)
    with pg.factory() as session:
        for model in (DocumentChunk, Job, Source):
            count = session.scalar(select(func.count()).select_from(model).where(model.owner_id == owner))
            assert count == 0, model.__name__


def test_api_roundtrip_on_postgres(pg):
    email = f"api-{uuid.uuid4().hex[:8]}@example.com"
    settings = pg.settings.model_copy(update={"dev_auth_enabled": True, "dev_user_email": email})
    try:
        with TestClient(create_app(settings)) as client:
            assert client.get("/ready").json()["database"] == "up"
            response = client.post(
                "/api/v1/sources/notes", json={"title": "PG API", "content": "retrieval over postgres"}
            )
            assert response.status_code == 201
            source_id = response.json()["source"]["id"]
            chunks = client.get(f"/api/v1/sources/{source_id}/chunks").json()
            assert chunks and chunks[0]["has_embedding"] is True
            assert client.delete(f"/api/v1/sources/{source_id}").status_code == 204
            assert client.get(f"/api/v1/sources/{source_id}").status_code == 404
    finally:
        with pg.factory() as session:
            session.execute(delete(User).where(User.email == email))
            session.commit()