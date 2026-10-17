import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.base import Base
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