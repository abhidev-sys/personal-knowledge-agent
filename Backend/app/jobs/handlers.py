"""Job handlers: the glue between the queue and the ingestion service."""
from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.jobs.queue import ClaimedJob

if TYPE_CHECKING:
    from app.services.ingestion_service import IngestionService

ServiceFactory = Callable[[Session], "IngestionService"]


class UrlIngestHandler:
    """Fetches a bookmarked page and indexes it."""

    def __init__(self, service_factory: ServiceFactory) -> None:
        self._service_factory = service_factory

    def run(self, session: Session, job: ClaimedJob) -> None:
        if job.source_id is None:
            return
        self._service_factory(session).process_bookmark(job.owner_id, job.source_id)

    def on_failure(
        self, session: Session, job: ClaimedJob, message: str, will_retry: bool
    ) -> None:
        if job.source_id is None:
            return
        self._service_factory(session).record_bookmark_failure(
            job.owner_id, job.source_id, message, will_retry=will_retry
        )