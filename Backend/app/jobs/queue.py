"""Database-backed job queue: enqueue, claim (safe with several workers), retry, recover."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import JobStatus
from app.db.models import Job
from app.db.types import utcnow

JOB_INGEST_URL = "ingest_url"
MAX_ERROR_LENGTH = 1000
MAX_BACKOFF_SECONDS = 3600.0
STALE_GIVE_UP_MESSAGE = "The worker stopped repeatedly while processing this; giving up."
STALE_RETRY_MESSAGE = "The worker stopped before finishing; retrying."


@dataclass(frozen=True)
class ClaimedJob:
    """A plain copy of a job, safe to use after its database session is closed."""

    id: uuid.UUID
    owner_id: uuid.UUID
    source_id: uuid.UUID | None
    job_type: str
    payload: dict[str, Any] | None
    attempts: int
    max_attempts: int


def _snapshot(job: Job) -> ClaimedJob:
    return ClaimedJob(
        id=job.id,
        owner_id=job.owner_id,
        source_id=job.source_id,
        job_type=job.job_type,
        payload=dict(job.payload) if job.payload else None,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
    )


def backoff_seconds(attempt: int, base: float) -> float:
    """Delay before the next try: base, 2*base, 4*base ... capped at one hour."""
    return min(MAX_BACKOFF_SECONDS, base * (2 ** max(0, attempt - 1)))


def enqueue(
    session: Session,
    *,
    owner_id: uuid.UUID,
    source_id: uuid.UUID | None,
    job_type: str,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> Job:
    """Add a job in the caller's transaction (flushes, does not commit).

    A job of the same type for the same source that is still waiting is reused, so
    pressing "refresh" ten times queues one job, not ten.
    """
    existing = session.scalar(
        select(Job).where(
            Job.owner_id == owner_id,
            Job.source_id == source_id,
            Job.job_type == job_type,
            Job.status == JobStatus.PENDING,
        )
    )
    if existing is not None:
        return existing
    job = Job(
        owner_id=owner_id,
        source_id=source_id,
        job_type=job_type,
        payload=payload,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def claim_next(
    session: Session, *, worker_id: str, now: datetime | None = None
) -> ClaimedJob | None:
    """Take the oldest due job. SKIP LOCKED lets several workers run without clashing."""
    now = now or utcnow()
    job = session.scalar(
        select(Job)
        .where(
            Job.status == JobStatus.PENDING,
            Job.run_after <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        session.commit()
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    session.commit()
    return _snapshot(job)


def complete(session: Session, job_id: uuid.UUID, *, now: datetime | None = None) -> None:
    job = session.get(Job, job_id)
    if job is None:  # its source was deleted meanwhile (the job cascaded away)
        return
    job.status = JobStatus.COMPLETED
    job.finished_at = now or utcnow()
    job.locked_at = None
    job.locked_by = None
    session.commit()


def fail(
    session: Session,
    job_id: uuid.UUID,
    message: str,
    *,
    retryable: bool,
    retry_base_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Record a failure. Returns True when the job was scheduled for another try."""
    job = session.get(Job, job_id)
    if job is None:
        return False
    now = now or utcnow()
    job.last_error = message[:MAX_ERROR_LENGTH]
    job.locked_at = None
    job.locked_by = None
    will_retry = retryable and job.attempts < job.max_attempts
    if will_retry:
        job.status = JobStatus.PENDING
        job.run_after = now + timedelta(seconds=backoff_seconds(job.attempts, retry_base_seconds))
    else:
        job.status = JobStatus.FAILED
        job.finished_at = now
    session.commit()
    return will_retry


def recover_stale_jobs(
    session: Session, *, lock_timeout_seconds: float, now: datetime | None = None
) -> list[ClaimedJob]:
    """Requeue jobs whose worker vanished. Returns the jobs that ran out of attempts (now FAILED)."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=lock_timeout_seconds)
    stale = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    given_up: list[ClaimedJob] = []
    for job in stale:
        job.locked_at = None
        job.locked_by = None
        if job.attempts < job.max_attempts:
            job.status = JobStatus.PENDING
            job.run_after = now
            job.last_error = STALE_RETRY_MESSAGE
        else:
            job.status = JobStatus.FAILED
            job.finished_at = now
            job.last_error = STALE_GIVE_UP_MESSAGE
            given_up.append(_snapshot(job))
    session.commit()
    return given_up