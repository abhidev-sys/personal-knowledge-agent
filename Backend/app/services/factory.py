"""Builds the service and its collaborators from settings (used by the worker, scripts and the API)."""
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.ingestion.fetcher import SafeFetcher
from app.ingestion.storage import FileStorage
from app.jobs.handlers import UrlIngestHandler
from app.providers.embeddings import EmbeddingProvider, HashingEmbedder
from app.services.ingestion_service import IngestionService


def build_embedder(settings: Settings) -> EmbeddingProvider | None:
    if settings.embedding_backend == "test-hashing":
        return HashingEmbedder()
    return None  # real providers arrive in Phase 16


def build_fetcher(settings: Settings) -> SafeFetcher:
    return SafeFetcher(
        timeout=settings.fetch_timeout_seconds,
        total_timeout=settings.fetch_total_timeout_seconds,
        max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects,
    )


def build_service_factory(
    settings: Settings, *, with_fetcher: bool
) -> Callable[[Session], IngestionService]:
    """Only the worker needs a fetcher; the web API never downloads pages itself."""
    storage = FileStorage(settings.upload_dir)
    embedder = build_embedder(settings)
    fetcher = build_fetcher(settings) if with_fetcher else None

    def make(session: Session) -> IngestionService:
        return IngestionService(session, settings, storage=storage, embedder=embedder, fetcher=fetcher)

    return make


def build_url_handler(settings: Settings) -> UrlIngestHandler:
    return UrlIngestHandler(build_service_factory(settings, with_fetcher=True))