"""Run the background worker (second terminal):  python scripts/run_worker.py   (Ctrl+C to stop)."""
import signal
import threading

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_sessionmaker
from app.jobs.queue import JOB_INGEST_URL
from app.jobs.worker import JobWorker
from app.services.factory import build_url_handler


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings.database_url)
    worker = JobWorker(
        build_sessionmaker(engine),
        {JOB_INGEST_URL: build_url_handler(settings)},
        lock_timeout_seconds=settings.job_lock_timeout_seconds,
        retry_base_seconds=settings.job_retry_base_seconds,
        poll_interval_seconds=settings.job_poll_interval_seconds,
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run_forever(stop)
    engine.dispose()


if __name__ == "__main__":
    main()