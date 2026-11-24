"""Demo of Phase 5B without the API: ingest, queue, run the worker, show the results.

Needs the repo-root .env (PKA_DATABASE_URL ...) and a migrated database.
    python scripts/try_ingest.py          (deletes its demo user and files afterwards)
    python scripts/try_ingest.py --keep
Optional: $env:PKA_EMBEDDING_BACKEND = "test-hashing"   (also exercises the pgvector write path)
"""
import shutil
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import Source, User
from app.db.session import build_engine, build_sessionmaker
from app.jobs.queue import JOB_INGEST_URL
from app.jobs.worker import JobWorker
from app.services.factory import build_service_factory, build_url_handler

NOTE = "Retrieval augmented generation lets a model answer from my own notes."
MARKDOWN = b"# Agents\nTools let a model act.\n\n## Memory\nAgents can remember user preferences.\n"


def main(keep: bool) -> int:
    settings = get_settings()
    configure_logging("WARNING")
    engine = build_engine(settings.database_url)
    factory = build_sessionmaker(engine)
    make_service = build_service_factory(settings, with_fetcher=True)

    with factory() as session:
        user = User(email=f"demo-{uuid.uuid4().hex[:8]}@example.com")
        session.add(user)
        session.commit()
        owner_id = user.id
    try:
        with factory() as session:
            service = make_service(session)
            first = service.create_note(owner_id, "RAG idea", NOTE, tags=["rag", "demo"])
            again = service.create_note(owner_id, "RAG idea", NOTE)
            service.create_from_upload(owner_id, "agents.md", MARKDOWN)
            service.create_bookmark(owner_id, "https://example.com")
            print(f"note created={first.created}; identical note again created={again.created}")

        worker = JobWorker(
            factory,
            {JOB_INGEST_URL: build_url_handler(settings)},
            retry_base_seconds=settings.job_retry_base_seconds,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and worker.run_once():
            pass

        with factory() as session:
            service = make_service(session)
            sources = session.scalars(select(Source).where(Source.owner_id == owner_id)).all()
            print(f"\n{'type':10} {'status':10} {'chunks':>6}  title")
            for s in service.summaries(sources):
                print(f"{s.source_type.value:10} {s.status.value:10} {s.chunk_count:>6}  {s.title}")
                if s.error_message:
                    print(f"           error: {s.error_message}")
    finally:
        if not keep:
            with factory() as session:
                session.execute(delete(User).where(User.id == owner_id))
                session.commit()
            shutil.rmtree(Path(settings.upload_dir) / str(owner_id), ignore_errors=True)
            print("\n(demo data removed; use --keep to keep it)")
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--keep" in sys.argv))