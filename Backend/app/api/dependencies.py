from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import session_scope


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)