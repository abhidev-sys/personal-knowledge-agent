from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool  # keep one in-memory DB across connections
        return create_engine(url, **kwargs)  # type: ignore[arg-type]
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def build_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One session per request; rolls back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()