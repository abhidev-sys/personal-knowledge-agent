"""Check that vectors really landed in pgvector, then remove the demo data left by --keep."""
import shutil
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import build_engine


def main() -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "select count(*), count(embedding), min(vector_dims(embedding)), "
                "min(embedding_model) from document_chunks"
            )
        ).one()
        print("chunks, with_vector, dimensions, model =", tuple(row))

        demo_ids = [
            str(r[0])
            for r in conn.execute(text("select id from users where email like 'demo-%'"))
        ]
        conn.execute(text("delete from users where email like 'demo-%'"))
        conn.commit()
        left = conn.execute(text("select count(*) from document_chunks")).scalar()
        print("demo users removed:", len(demo_ids), "| chunks left:", left)
    for owner_id in demo_ids:
        shutil.rmtree(Path(settings.upload_dir) / owner_id, ignore_errors=True)
    engine.dispose()


if __name__ == "__main__":
    main()