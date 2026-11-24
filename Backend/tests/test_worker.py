from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from app.core.exceptions import IngestionError, TransientIngestionError
from app.db.base import Base
from app.db.enums import IngestionStatus, JobStatus
from app.db.models import DocumentChunk, Job, Source, User
from app.db.session import build_engine, build_sessionmaker
from app.db.types import utcnow
from app.ingestion.storage import FileStorage
from app.jobs.handlers import UrlIngestHandler
from app.jobs.queue import JOB_INGEST_URL
from app.jobs.worker import JobWorker
from app.providers.embeddings import HashingEmbedder
from app.services.ingestion_service import IngestionService

URL = "https://example.com/a"
ARTICLE = (
    "<html><head><title>My Article</title></head><body><article><h1>Heading</h1><p>"
    + "Retrieval augmented generation combines search with language models. " * 3
    + "</p></article></body></html>"
)


@pytest.fixture
def env(tmp_path, settings, fake_fetcher):
    engine = build_engine(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = build_sessionmaker(engine)
    options = settings.model_copy(update={"job_retry_base_seconds": 0.0, "job_max_attempts": 2})
    storage = FileStorage(tmp_path / "files")

    def service_for(session):
        return IngestionService(
            session, options, storage=storage, embedder=HashingEmbedder(), fetcher=fake_fetcher
        )

    worker = JobWorker(
        factory,
        {JOB_INGEST_URL: UrlIngestHandler(service_for)},
        worker_id="test-worker",
        retry_base_seconds=0.0,
    )
    with factory() as session:
        user = User(email="w@example.com")
        session.add(user)
        session.commit()
        owner_id = user.id
    yield SimpleNamespace(
        factory=factory, worker=worker, service_for=service_for, owner_id=owner_id, fetcher=fake_fetcher
    )
    engine.dispose()


def queue_bookmark(env, url=URL):
    with env.factory() as session:
        return env.service_for(session).create_bookmark(env.owner_id, url).source.id


def state(env, source_id):
    with env.factory() as session:
        source = session.get(Source, source_id)
        job = session.scalar(select(Job).where(Job.source_id == source_id))
        chunks = session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.source_id == source_id)
        )
        return SimpleNamespace(
            status=source.status,
            error=source.error_message,
            title=source.title,
            chunks=chunks,
            job_status=job.status,
            attempts=job.attempts,
            last_error=job.last_error,
        )


def test_worker_indexes_a_bookmark_end_to_end(env):
    env.fetcher.add_html(URL, ARTICLE)
    source_id = queue_bookmark(env)
    assert env.worker.run_once() is True
    result = state(env, source_id)
    assert result.status == IngestionStatus.COMPLETED
    assert result.job_status == JobStatus.COMPLETED
    assert result.title == "My Article" and result.chunks >= 1
    assert env.worker.run_once() is False  # queue is empty now


def test_transient_error_is_retried_and_then_succeeds(env):
    env.fetcher.add(
        URL, TransientIngestionError("Site slow"), env.fetcher.html_page(URL, ARTICLE)
    )
    source_id = queue_bookmark(env)
    assert env.worker.run_once() is True
    first = state(env, source_id)
    assert first.job_status == JobStatus.PENDING and first.attempts == 1
    assert first.status == IngestionStatus.PENDING and "Retrying" in first.error
    assert "Site slow" in first.error
    assert env.worker.run_once() is True
    second = state(env, source_id)
    assert second.status == IngestionStatus.COMPLETED and second.job_status == JobStatus.COMPLETED
    assert second.attempts == 2


def test_permanent_error_fails_immediately(env):
    env.fetcher.add(URL, IngestionError("The site returned HTTP 404."))
    source_id = queue_bookmark(env)
    env.worker.run_once()
    result = state(env, source_id)
    assert result.status == IngestionStatus.FAILED and "404" in result.error
    assert result.job_status == JobStatus.FAILED and result.attempts == 1
    assert env.worker.run_once() is False


def test_worker_gives_up_after_the_last_attempt(env):
    env.fetcher.add(URL, TransientIngestionError("slow"))
    source_id = queue_bookmark(env)
    env.worker.run_once()
    env.worker.run_once()
    result = state(env, source_id)
    assert result.job_status == JobStatus.FAILED and result.attempts == 2
    assert result.status == IngestionStatus.FAILED and "slow" in result.error
    assert env.worker.run_once() is False


def test_unexpected_errors_are_retried_without_leaking_their_text(env):
    env.fetcher.add(URL, RuntimeError("secret document text"))
    source_id = queue_bookmark(env)
    env.worker.run_once()
    result = state(env, source_id)
    assert result.job_status == JobStatus.PENDING
    assert "RuntimeError" in result.last_error and "secret" not in result.last_error
    assert "secret" not in result.error


def test_unknown_job_type_fails_without_retry(env):
    with env.factory() as session:
        session.add(Job(owner_id=env.owner_id, job_type="nope"))
        session.commit()
    assert env.worker.run_once() is True
    with env.factory() as session:
        job = session.scalar(select(Job))
        assert job.status == JobStatus.FAILED and "No handler" in job.last_error


def test_job_abandoned_by_a_dead_worker_is_recovered_and_finished(env):
    env.fetcher.add_html(URL, ARTICLE)
    source_id = queue_bookmark(env)
    with env.factory() as session:
        session.execute(
            update(Job).values(
                status=JobStatus.RUNNING,
                attempts=1,
                locked_at=utcnow() - timedelta(hours=2),
                locked_by="dead-worker",
            )
        )
        session.commit()
    assert env.worker.run_once() is True
    result = state(env, source_id)
    assert result.status == IngestionStatus.COMPLETED and result.job_status == JobStatus.COMPLETED
    assert result.attempts == 2