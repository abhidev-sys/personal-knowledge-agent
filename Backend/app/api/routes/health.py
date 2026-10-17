import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies import get_db

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness: dependencies (the database) are reachable."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        log.error("readiness_db_failed", exc_info=True)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    return JSONResponse(content={"status": "ready", "database": "up"})