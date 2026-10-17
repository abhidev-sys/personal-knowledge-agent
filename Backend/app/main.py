from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import build_engine, build_sessionmaker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = build_sessionmaker(engine)
        app.state.settings = settings
        yield
        engine.dispose()

    app = FastAPI(title="Personal Knowledge Agent", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,  # explicit list only
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "x-request-id"],
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()