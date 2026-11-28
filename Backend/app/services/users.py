"""Temporary user handling until real authentication arrives (Phase 8)."""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import PermissionDeniedError
from app.db.models import User, UserSettings


def get_or_create_dev_user(session: Session, email: str) -> User:
    """Return the local development user, creating it (with default, private settings) once."""
    normalized = email.strip().lower()
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        user = User(email=normalized)
        session.add(user)
        try:
            session.flush()
            session.add(UserSettings(user_id=user.id))
            session.commit()
        except IntegrityError:  # another request created it first
            session.rollback()
            user = session.scalar(select(User).where(User.email == normalized))
            if user is None:
                raise
    if not user.is_active:
        raise PermissionDeniedError("This account is disabled.")
    if session.get(UserSettings, user.id) is None:
        session.add(UserSettings(user_id=user.id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    return user