"""Shared column types.

UTCDateTime       - always timezone-aware UTC (SQLite would otherwise return naive datetimes).
EmbeddingVector   - pgvector `vector(768)` on PostgreSQL, JSON on SQLite (tests only).
enum_column       - Python Enum stored as VARCHAR + named CHECK constraint.
"""
import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

# Changing this requires a new migration AND re-embedding every chunk.
EMBEDDING_DIM = 768


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime rejected: use timezone-aware UTC datetimes")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class EmbeddingVector(TypeDecorator[list[float]]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector  # lazy: SQLite tests don't need it

            return dialect.type_descriptor(Vector(EMBEDDING_DIM))
        return dialect.type_descriptor(JSON(none_as_null=True))

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if len(value) != EMBEDDING_DIM:
            raise ValueError(f"Embedding must have {EMBEDDING_DIM} dimensions, got {len(value)}")
        return [float(x) for x in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        return [float(x) for x in value]  # pgvector may return a numpy array


def enum_column(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Store the enum's *values* as VARCHAR(32) guarded by CHECK constraint ck_<table>_<name>."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )