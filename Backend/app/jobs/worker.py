"""Polling worker: claims due jobs, runs their handler, records the outcome."""
import logging
import os
import socket
import threading
import time
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppError, TransientIngestionError
from app.jobs.queue import (
    STALE_GIVE_UP_MESSAGE,
    ClaimedJob,
    claim_next,
    complete,
    fail,
    recover_stale_jobs,
)

log = logging.getLogger("pka.jobs")


class JobHandler(Protocol):
    def run(self, session: Session, job: ClaimedJob) -> None: ...

    def on_failure(
        self, session: Session, job: ClaimedJob, message: str, will_retry: bool
    ) -> None: ...


class JobWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str | None = None,
        lock_timeout_seconds: float = 900.0,
        retry_base_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._factory = session_factory
        self._handlers = handlers
        self._worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._lock_timeout = lock_timeout_seconds
        self._retry_base = retry_base_seconds
        self._poll_interval = poll_interval_seconds

    def run_once(self) -> bool:
        """Process at most one job. Returns False when nothing was due."""
        with self._factory() as session:
            given_up = recover_stale_jobs(session, lock_timeout_seconds=self._lock_timeout)
            job = claim_next(session, worker_id=self._worker_id)
        for dead in given_up:
            self._notify_failure(dead, STALE_GIVE_UP_MESSAGE, will_retry=False)
        if job is None:
            return False

        started = time.monotonic()
        handler = self._handlers.get(job.job_type)
        if handler is None:
            self._record_failure(
                job, f"No handler is registered for job type '{job.job_type}'.", retryable=False
            )
            return True
        try:
            with self._factory() as session:
                handler.run(session, job)
        except TransientIngestionError as exc:
            self._record_failure(job, exc.message, retryable=True)
        except AppError as exc:
            self._record_failure(job, exc.message, retryable=False)
        except Exception as exc:
            log.error("job_crashed", exc_info=True, extra={"ctx": self._ctx(job, started)})
            # Only the exception TYPE is stored: its text could contain document content.
            self._record_failure(job, f"Unexpected error ({type(exc).__name__}).", retryable=True)
        else:
            with self._factory() as session:
                complete(session, job.id)
            log.info("job_completed", extra={"ctx": self._ctx(job, started, "completed")})
        return True

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("worker_started", extra={"ctx": {"worker_id": self._worker_id}})
        while not stop.is_set():
            try:
                processed = self.run_once()
            except Exception:  # e.g. the database is briefly unreachable
                log.error("worker_iteration_failed", exc_info=True)
                processed = False
            if not processed:
                stop.wait(self._poll_interval)
        log.info("worker_stopped", extra={"ctx": {"worker_id": self._worker_id}})

    # ------------------------------------------------------------------------ internals
    def _record_failure(self, job: ClaimedJob, message: str, *, retryable: bool) -> None:
        with self._factory() as session:
            will_retry = fail(
                session, job.id, message, retryable=retryable, retry_base_seconds=self._retry_base
            )
        outcome = "retry_scheduled" if will_retry else "failed"
        log.warning("job_failed", extra={"ctx": self._ctx(job, None, outcome)})
        self._notify_failure(job, message, will_retry)

    def _notify_failure(self, job: ClaimedJob, message: str, will_retry: bool) -> None:
        handler = self._handlers.get(job.job_type)
        if handler is None:
            return
        try:
            with self._factory() as session:
                handler.on_failure(session, job, message, will_retry)
        except Exception:
            log.error("failure_notification_failed", exc_info=True)

    @staticmethod
    def _ctx(job: ClaimedJob, started: float | None, outcome: str | None = None) -> dict[str, object]:
        ctx: dict[str, object] = {
            "job_id": str(job.id),
            "job_type": job.job_type,
            "attempt": job.attempts,
        }
        if started is not None:
            ctx["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        if outcome:
            ctx["outcome"] = outcome
        return ctx