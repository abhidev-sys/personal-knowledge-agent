import uuid
from datetime import timedelta

import pytest

from app.db.enums import JobStatus
from app.db.models import Job
from app.db.types import utcnow
from app.jobs.queue import (
    backoff_seconds,
    claim_next,
    complete,
    enqueue,
    fail,
    recover_stale_jobs,
)


def add_job(session, owner, **overrides) -> Job:
    options = {"owner_id": owner.id, "source_id": None, "job_type": "x"}
    options.update(overrides)
    job = enqueue(session, **options)
    session.commit()
    return job


@pytest.mark.parametrize(
    "attempt,expected", [(1, 30), (2, 60), (3, 120), (4, 240), (30, 3600)]
)
def test_backoff_doubles_and_is_capped(attempt, expected):
    assert backoff_seconds(attempt, 30) == expected


def test_enqueue_reuses_a_waiting_job(db_session, owner):
    first = add_job(db_session, owner)
    second = add_job(db_session, owner)
    assert first.id == second.id


def test_claim_marks_the_job_running_and_counts_the_attempt(db_session, owner):
    add_job(db_session, owner, payload={"k": 1})
    claimed = claim_next(db_session, worker_id="w1")
    assert claimed is not None
    assert claimed.job_type == "x" and claimed.attempts == 1 and claimed.payload == {"k": 1}
    job = db_session.get(Job, claimed.id)
    assert job.status == JobStatus.RUNNING and job.locked_by == "w1"
    assert claim_next(db_session, worker_id="w1") is None


def test_a_job_scheduled_for_later_is_not_claimed_yet(db_session, owner):
    job = add_job(db_session, owner)
    job.run_after = utcnow() + timedelta(hours=1)
    db_session.commit()
    assert claim_next(db_session, worker_id="w") is None
    assert claim_next(db_session, worker_id="w", now=utcnow() + timedelta(hours=2)) is not None


def test_retryable_failure_is_rescheduled_with_backoff(db_session, owner):
    add_job(db_session, owner)
    claimed = claim_next(db_session, worker_id="w")
    now = utcnow()
    assert fail(db_session, claimed.id, "temporary", retryable=True, retry_base_seconds=30, now=now)
    job = db_session.get(Job, claimed.id)
    assert job.status == JobStatus.PENDING and job.last_error == "temporary"
    assert job.run_after == now + timedelta(seconds=30)
    assert job.locked_by is None


def test_permanent_failure_is_not_retried(db_session, owner):
    add_job(db_session, owner)
    claimed = claim_next(db_session, worker_id="w")
    assert fail(db_session, claimed.id, "bad input", retryable=False, retry_base_seconds=30) is False
    job = db_session.get(Job, claimed.id)
    assert job.status == JobStatus.FAILED and job.finished_at is not None


def test_job_fails_for_good_when_attempts_run_out(db_session, owner):
    add_job(db_session, owner, max_attempts=2)
    first = claim_next(db_session, worker_id="w")
    assert fail(db_session, first.id, "again", retryable=True, retry_base_seconds=0) is True
    second = claim_next(db_session, worker_id="w", now=utcnow() + timedelta(seconds=1))
    assert second is not None and second.attempts == 2
    assert fail(db_session, second.id, "again", retryable=True, retry_base_seconds=0) is False
    assert db_session.get(Job, second.id).status == JobStatus.FAILED


def test_complete_marks_the_job_done(db_session, owner):
    add_job(db_session, owner)
    claimed = claim_next(db_session, worker_id="w")
    complete(db_session, claimed.id)
    job = db_session.get(Job, claimed.id)
    assert job.status == JobStatus.COMPLETED and job.finished_at is not None


def test_finishing_a_vanished_job_is_harmless(db_session):
    missing = uuid.uuid4()
    complete(db_session, missing)
    assert fail(db_session, missing, "x", retryable=True, retry_base_seconds=0) is False


def test_stale_running_job_is_requeued(db_session, owner):
    job = add_job(db_session, owner)
    job.status = JobStatus.RUNNING
    job.attempts = 1
    job.locked_at = utcnow() - timedelta(hours=2)
    job.locked_by = "dead-worker"
    db_session.commit()
    assert recover_stale_jobs(db_session, lock_timeout_seconds=900) == []
    db_session.refresh(job)
    assert job.status == JobStatus.PENDING and job.locked_by is None


def test_stale_job_without_attempts_left_fails_and_is_reported(db_session, owner):
    job = add_job(db_session, owner, max_attempts=3)
    job.status = JobStatus.RUNNING
    job.attempts = 3
    job.locked_at = utcnow() - timedelta(hours=2)
    db_session.commit()
    given_up = recover_stale_jobs(db_session, lock_timeout_seconds=900)
    assert [j.id for j in given_up] == [job.id]
    db_session.refresh(job)
    assert job.status == JobStatus.FAILED


def test_recent_running_job_is_left_alone(db_session, owner):
    job = add_job(db_session, owner)
    job.status = JobStatus.RUNNING
    job.attempts = 1
    job.locked_at = utcnow()
    db_session.commit()
    assert recover_stale_jobs(db_session, lock_timeout_seconds=900) == []
    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING