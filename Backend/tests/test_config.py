import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_secret_key_fails():
    with pytest.raises(ValidationError):
        Settings(environment="test", database_url="sqlite://", secret_key="short")


def test_production_rejects_wildcard_origin():
    with pytest.raises(ValidationError):
        Settings(environment="production", database_url="postgresql+psycopg://u:p@h/db",
                 secret_key="x" * 40, allowed_origins=["*"])


def test_production_rejects_sqlite():
    with pytest.raises(ValidationError):
        Settings(environment="production", database_url="sqlite://", secret_key="x" * 40)
    
    
def test_chunk_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValidationError):
        Settings(
            environment="test",
            database_url="sqlite://",
            secret_key="x" * 40,
            chunk_size_chars=200,
            chunk_overlap_chars=200,
        )