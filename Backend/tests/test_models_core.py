import hashlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.enums import JobStatus, MemoryMode, SourceType
from app.db.models import DocumentChunk, Job, Source, SourceTag, Tag, User, UserSettings
from app.db.types import EMBEDDING_DIM


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def make_user(session: Session, email: str | None = None) -> User:
    user = User(email=email or f"{uuid.uuid4().hex[:10]}@example.com")
    session.add(user)
    session.flush()
    return user


def make_source(session: Session, owner: User, **overrides) -> Source:
    fields = dict(
        owner_id=owner.id,
        source_type=SourceType.NOTE,
        title="A note",
        content_text="hello",
        content_hash=sha(uuid.uuid4().hex),
    )
    fields.update(overrides)
    source = Source(**fields)
    session.add(source)
    session.flush()
    return source


def make_chunk(session: Session, source: Source, sequence: int = 0, **overrides) -> DocumentChunk:
    fields = dict(
        source_id=source.id,
        owner_id=source.owner_id,
        sequence=sequence,
        content=f"chunk {sequence}",
        content_hash=sha(f"chunk {sequence}"),
    )
    fields.update(overrides)
    chunk = DocumentChunk(**fields)
    session.add(chunk)
    session.flush()
    return chunk


# ------------------------------------------------------------- schema / users / settings
def test_core_tables_exist(db_session: Session):
    names = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "users", "user_settings", "tags", "sources", "source_tags", "document_chunks", "jobs"
    } <= names


def test_email_must_be_lowercase(db_session: Session):
    with pytest.raises(IntegrityError):
        make_user(db_session, "Mixed@Example.com")


def test_duplicate_email_rejected(db_session: Session):
    make_user(db_session, "a@example.com")
    with pytest.raises(IntegrityError):
        make_user(db_session, "a@example.com")


def test_user_settings_defaults_are_private(db_session: Session):
    user = make_user(db_session)
    settings = UserSettings(user_id=user.id)
    db_session.add(settings)
    db_session.flush()
    db_session.refresh(settings)
    assert settings.allow_external_processing is False
    assert settings.llm_provider == "ollama"
    assert settings.memory_mode == MemoryMode.SUGGEST
    assert settings.digest_period_days == 7
    assert settings.weekly_digest_enabled is False


def test_user_settings_rejects_bad_digest_period(db_session: Session):
    user = make_user(db_session)
    db_session.add(UserSettings(user_id=user.id, digest_period_days=0))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ------------------------------------------------------------------------------- sources
def test_duplicate_content_is_rejected_per_owner_only(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    h = sha("same text")
    make_source(db_session, alice, content_hash=h)
    make_source(db_session, bob, content_hash=h)  # a different owner may hold the same content
    with pytest.raises(IntegrityError):
        make_source(db_session, alice, content_hash=h)


def test_bookmark_requires_url(db_session: Session):
    user = make_user(db_session)
    with pytest.raises(IntegrityError):
        make_source(db_session, user, source_type=SourceType.BOOKMARK, original_url=None)


def test_canonical_url_unique_per_owner(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    url = "https://example.com/article"
    kw = dict(source_type=SourceType.BOOKMARK, original_url=url, canonical_url=url)
    make_source(db_session, alice, **kw)
    make_source(db_session, bob, **kw)
    with pytest.raises(IntegrityError):
        make_source(db_session, alice, **kw)


# -------------------------------------------------------------------------------- chunks
def test_chunk_cannot_point_at_another_owners_source(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    alices_source = make_source(db_session, alice)
    with pytest.raises(IntegrityError):
        make_chunk(db_session, alices_source, owner_id=bob.id)  # wrong owner -> composite FK fails


def test_chunk_sequence_unique_per_source(db_session: Session):
    source = make_source(db_session, make_user(db_session))
    make_chunk(db_session, source, 0)
    with pytest.raises(IntegrityError):
        make_chunk(db_session, source, 0)


def test_embedding_roundtrip_and_null(db_session: Session):
    source = make_source(db_session, make_user(db_session))
    with_vec = make_chunk(
        db_session, source, 0, embedding=[0.1] * EMBEDDING_DIM, embedding_model="m"
    )
    make_chunk(db_session, source, 1, embedding=None)
    db_session.refresh(with_vec)
    assert len(with_vec.embedding) == EMBEDDING_DIM
    assert with_vec.embedding[0] == pytest.approx(0.1)
    null_rows = db_session.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.embedding.is_(None))
    )
    assert null_rows == 1  # None is stored as SQL NULL, not JSON "null"


def test_embedding_requires_model(db_session: Session):
    source = make_source(db_session, make_user(db_session))
    with pytest.raises(IntegrityError):
        make_chunk(db_session, source, 0, embedding=[0.1] * EMBEDDING_DIM, embedding_model=None)


def test_embedding_dimension_enforced(db_session: Session):
    source = make_source(db_session, make_user(db_session))
    with pytest.raises(StatementError):
        make_chunk(db_session, source, 0, embedding=[0.1, 0.2], embedding_model="m")


def test_embedding_dim_matches_migration():
    migration = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0002_core_schema.py"
    )
    assert f"EMBEDDING_DIM = {EMBEDDING_DIM}" in migration.read_text()


# ----------------------------------------------------------------------------- timestamps
def test_timestamps_roundtrip_as_utc(db_session: Session):
    source = make_source(db_session, make_user(db_session))
    db_session.refresh(source)
    assert source.created_at.utcoffset() == timedelta(0)


def test_naive_datetime_rejected(db_session: Session):
    user = make_user(db_session)
    with pytest.raises(StatementError):
        make_source(db_session, user, last_synced_at=datetime.now())  # no tzinfo


# ------------------------------------------------------------------ deletion / cascades
def test_deleting_source_cascades(db_session: Session):
    user = make_user(db_session)
    source = make_source(db_session, user)
    make_chunk(db_session, source, 0)
    make_chunk(db_session, source, 1)
    tag = Tag(owner_id=user.id, name="rag")
    db_session.add(tag)
    db_session.flush()
    db_session.add(SourceTag(source_id=source.id, tag_id=tag.id, owner_id=user.id))
    db_session.add(Job(owner_id=user.id, source_id=source.id, job_type="ingest_source"))
    db_session.commit()

    db_session.delete(source)
    db_session.commit()

    assert count(db_session, DocumentChunk) == 0
    assert count(db_session, SourceTag) == 0
    assert count(db_session, Job) == 0
    assert count(db_session, Tag) == 1  # the tag belongs to the user, not the source


def test_deleting_user_cascades(db_session: Session):
    user = make_user(db_session)
    db_session.add(UserSettings(user_id=user.id))
    source = make_source(db_session, user)
    make_chunk(db_session, source, 0)
    db_session.commit()

    db_session.delete(user)
    db_session.commit()

    for model in (UserSettings, Source, DocumentChunk):
        assert count(db_session, model) == 0


def test_job_attempt_limits(db_session: Session):
    user = make_user(db_session)
    db_session.add(Job(owner_id=user.id, job_type="x", attempts=5, max_attempts=3))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    user2 = make_user(db_session)
    job = Job(owner_id=user2.id, job_type="x")
    db_session.add(job)
    db_session.flush()
    assert job.status == JobStatus.PENDING