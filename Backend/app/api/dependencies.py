from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AuthenticationRequiredError
from app.db.models import User
from app.db.session import session_scope
from app.services.ingestion_service import IngestionService
from app.services.users import get_or_create_dev_user


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> User:
    """Who is calling. Until real login exists (Phase 8) this is a single local development user,
    and only when PKA_DEV_AUTH_ENABLED=true. Otherwise every request is refused with 401."""
    if not settings.dev_auth_enabled:
        raise AuthenticationRequiredError("Authentication is required.")
    return get_or_create_dev_user(db, settings.dev_user_email)


def get_ingestion_service(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> IngestionService:
    # No fetcher on purpose: the web API never downloads pages. Only the worker does.
    return IngestionService(
        db,
        settings,
        storage=request.app.state.storage,
        embedder=request.app.state.embedder,
    )