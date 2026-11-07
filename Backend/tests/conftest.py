from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import models  # noqa: F401  (registers all tables on Base.metadata)
from app.db.base import Base
from app.db.session import build_engine, build_sessionmaker
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite://",
        secret_key="x" * 40,
        allowed_origins=["http://localhost:8501"],
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