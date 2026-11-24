from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import TransientIngestionError
from app.db import models  # noqa: F401  (registers all tables on Base.metadata)
from app.db.base import Base
from app.db.models import User
from app.db.session import build_engine, build_sessionmaker
from app.ingestion.fetcher import FetchedPage
from app.ingestion.storage import FileStorage
from app.main import create_app
from app.providers.embeddings import HashingEmbedder
from app.services.ingestion_service import IngestionService


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite://",
        secret_key="x" * 40,
        allowed_origins=["http://localhost:8501"],
        upload_dir=str(tmp_path / "uploads"),
        dev_auth_enabled=True,
        embedding_backend="test-hashing",
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        Base.metadata.create_all(app.state.engine)
        yield c


@pytest.fixture
def db_session(settings: Settings) -> Iterator[Session]:
    """A fresh in-memory SQLite database (foreign keys ON) per test."""
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with build_sessionmaker(engine)() as session:
        yield session
    engine.dispose()


class FakeFetcher:
    """Stands in for SafeFetcher: returns canned pages or raises canned errors.

    add(url, r1, r2, ...) answers successive calls with r1, r2, ... and repeats the last one.
    """

    def __init__(self) -> None:
        self.responses: dict[str, list[object]] = {}
        self.calls: list[str] = []

    @staticmethod
    def html_page(
        url: str, html: str, *, content_type: str = "text/html", final_url: str | None = None
    ) -> FetchedPage:
        return FetchedPage(
            final_url=final_url or url,
            content=html.encode("utf-8"),
            content_type=content_type,
            fetched_at=datetime.now(timezone.utc),
        )

    def add(self, url: str, *responses: object) -> None:
        self.responses[url] = list(responses)

    def add_html(self, url: str, html: str, *, final_url: str | None = None) -> None:
        self.add(url, self.html_page(url, html, final_url=final_url))

    def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        queue = self.responses.get(url)
        if not queue:
            raise TransientIngestionError("No canned response for this URL.")
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, FetchedPage)
        return item


@pytest.fixture
def storage(tmp_path) -> FileStorage:
    return FileStorage(tmp_path / "files")


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher()


def _make_user(session: Session, email: str) -> User:
    user = User(email=email)
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def owner(db_session: Session) -> User:
    return _make_user(db_session, "owner@example.com")


@pytest.fixture
def other_user(db_session: Session) -> User:
    return _make_user(db_session, "other@example.com")


@pytest.fixture
def make_service(db_session, settings, storage, fake_fetcher):
    def build(**overrides) -> IngestionService:
        options = {"embedder": HashingEmbedder(), "fetcher": fake_fetcher}
        use_settings = overrides.pop("settings", settings)
        options.update(overrides)
        return IngestionService(db_session, use_settings, storage=storage, **options)

    return build


@pytest.fixture
def service(make_service) -> IngestionService:
    return make_service()