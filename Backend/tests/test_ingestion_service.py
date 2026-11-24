import pymupdf
import pytest
from sqlalchemy import func, select

from app.core.exceptions import (
    ConflictError,
    IngestionError,
    InvalidInputError,
    NotFoundError,
    PayloadTooLargeError,
    UnsafeUrlError,
)
from app.db.enums import IngestionStatus, JobStatus, SourceType
from app.db.models import DocumentChunk, Job, Source, SourceTag, Tag
from app.jobs.queue import JOB_INGEST_URL
from app.providers.embeddings import HashingEmbedder
from app.services.ingestion_service import normalize_tags, safe_filename

RAG_TEXT = "Retrieval augmented generation combines search with language models. " * 3
ARTICLE = (
    "<html><head><title>My Article</title></head><body><article><h1>Heading</h1><p>"
    + RAG_TEXT
    + "</p></article></body></html>"
)


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def chunks_of(session, source):
    return session.scalars(
        select(DocumentChunk).where(DocumentChunk.source_id == source.id).order_by(DocumentChunk.sequence)
    ).all()


class BrokenEmbedder(HashingEmbedder):
    def embed(self, texts):
        raise RuntimeError("provider down")


# ================================================================== pure helpers
def test_normalize_tags_lowercases_trims_and_dedupes():
    assert normalize_tags([" RAG ", "rag", "AI  Agents", ""]) == ["rag", "ai agents"]
    assert normalize_tags(None) == []


def test_normalize_tags_limits():
    with pytest.raises(InvalidInputError):
        normalize_tags(["x" * 65])
    with pytest.raises(InvalidInputError):
        normalize_tags([f"t{i}" for i in range(21)])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../evil.md", "evil.md"),
        ("C:\\dir\\file.txt", "file.txt"),
        ("..", "upload"),
        (None, "upload"),
        ("we<ir>d.md", "weird.md"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


# ======================================================================== notes
def test_note_is_chunked_embedded_and_tagged(service, owner, db_session):
    result = service.create_note(owner.id, "RAG notes", RAG_TEXT, tags=["RAG", " rag ", "AI"])
    source = result.source
    assert result.created is True
    assert source.status == IngestionStatus.COMPLETED
    assert source.source_type == SourceType.NOTE
    chunks = chunks_of(db_session, source)
    assert chunks
    assert all(c.embedding is not None and c.embedding_model == "test-hashing-768" for c in chunks)
    assert all(source.content_text[c.char_start : c.char_end] == c.content for c in chunks)
    (summary,) = service.summaries([source])
    assert summary.tags == ["ai", "rag"]
    assert summary.chunk_count == len(chunks)


def test_identical_note_is_not_stored_twice(service, owner, db_session):
    first = service.create_note(owner.id, "Same", RAG_TEXT)
    second = service.create_note(owner.id, "Same", RAG_TEXT)
    assert first.created is True and second.created is False
    assert second.source.id == first.source.id
    assert count(db_session, Source) == 1


def test_same_text_with_another_title_is_a_new_note(service, owner, db_session):
    service.create_note(owner.id, "One", RAG_TEXT)
    assert service.create_note(owner.id, "Two", RAG_TEXT).created is True
    assert count(db_session, Source) == 2


def test_the_same_note_can_exist_for_two_users(service, owner, other_user):
    assert service.create_note(owner.id, "N", RAG_TEXT).created is True
    assert service.create_note(other_user.id, "N", RAG_TEXT).created is True


def test_blank_note_is_rejected(service, owner):
    with pytest.raises(InvalidInputError):
        service.create_note(owner.id, "t", "  \n \t ")


def test_note_without_title_uses_its_first_line(service, owner):
    source = service.create_note(owner.id, None, "First line here\nsecond line").source
    assert source.title == "First line here"


def test_oversized_text_is_rejected(make_service, owner, settings):
    small = make_service(settings=settings.model_copy(update={"max_text_chars": 1000}))
    with pytest.raises(PayloadTooLargeError):
        small.create_note(owner.id, "big", "word " * 400)


def test_editing_a_note_replaces_its_chunks(service, owner, db_session):
    source = service.create_note(owner.id, "N", "alpha beta gamma").source
    old_ids = {c.id for c in chunks_of(db_session, source)}
    service.update_source(owner.id, source.id, content="delta epsilon zeta")
    new_chunks = chunks_of(db_session, source)
    assert {c.id for c in new_chunks}.isdisjoint(old_ids)
    assert all("alpha" not in c.content for c in new_chunks)
    assert any("delta" in c.content for c in new_chunks)
    assert source.content_text == "delta epsilon zeta"


def test_renaming_a_note_keeps_its_chunks(service, owner, db_session):
    source = service.create_note(owner.id, "Old", "alpha beta gamma").source
    old_ids = {c.id for c in chunks_of(db_session, source)}
    old_hash = source.content_hash
    service.update_source(owner.id, source.id, title="New")
    assert source.title == "New"
    assert source.content_hash != old_hash
    assert {c.id for c in chunks_of(db_session, source)} == old_ids


def test_editing_a_note_into_a_duplicate_is_a_conflict(service, owner, db_session):
    first = service.create_note(owner.id, "A", "alpha text").source
    second = service.create_note(owner.id, "B", "beta text").source
    with pytest.raises(ConflictError):
        service.update_source(owner.id, second.id, title="A", content="alpha text")
    db_session.refresh(second)
    assert second.title == "B" and first.title == "A"


def test_only_notes_can_be_edited_but_any_source_can_be_tagged(service, owner):
    source = service.create_from_upload(owner.id, "a.md", b"# T\nbody text").source
    with pytest.raises(InvalidInputError):
        service.update_source(owner.id, source.id, content="changed")
    service.update_source(owner.id, source.id, tags=["Reading", "later"])
    assert service.summaries([source])[0].tags == ["later", "reading"]


# ================================================================= uploaded files
def test_markdown_upload_keeps_headings_and_the_original_file(service, owner, db_session, storage):
    data = b"# Guide\nintro text here\n\n## Setup\nrun the installer\n"
    source = service.create_from_upload(owner.id, "guide.md", data).source
    assert source.source_type == SourceType.MARKDOWN
    assert source.title == "Guide"
    assert {c.section_heading for c in chunks_of(db_session, source)} == {"Guide", "Guide > Setup"}
    assert storage.read(source.storage_path) == data
    assert source.original_filename == "guide.md"


def test_text_upload_supports_hindi(service, owner):
    text = "यह एक परीक्षण है। नमस्ते दुनिया।"
    source = service.create_from_upload(owner.id, "notes.txt", text.encode("utf-8")).source
    assert source.source_type == SourceType.TEXT
    assert text in source.content_text
    assert source.title == "notes"


def test_pdf_upload_keeps_page_numbers(service, owner, db_session, storage):
    data = make_pdf(["Retrieval augmented generation", "Vector databases"])
    source = service.create_from_upload(owner.id, "rag.pdf", data).source
    assert source.source_type == SourceType.PDF
    assert source.page_count == 2
    assert {c.page_number for c in chunks_of(db_session, source)} == {1, 2}
    assert storage.read(source.storage_path) == data


def test_image_only_pdf_is_reported_not_faked(service, owner, db_session):
    with pytest.raises(IngestionError, match="OCR"):
        service.create_from_upload(owner.id, "scan.pdf", make_pdf(["", ""]))
    assert count(db_session, Source) == 0


@pytest.mark.parametrize(
    "name,data,error",
    [
        ("tool.exe", b"MZ", InvalidInputError),
        ("empty.md", b"", InvalidInputError),
        ("noextension", b"hello", InvalidInputError),
        ("fake.pdf", b"just text", IngestionError),
        ("binary.txt", b"\x80\x81\xff", IngestionError),
    ],
)
def test_bad_uploads_are_rejected(service, owner, name, data, error):
    with pytest.raises(error):
        service.create_from_upload(owner.id, name, data)


def test_oversized_upload_is_rejected(make_service, owner, settings):
    small = make_service(settings=settings.model_copy(update={"max_upload_bytes": 1024}))
    with pytest.raises(PayloadTooLargeError):
        small.create_from_upload(owner.id, "big.txt", b"a" * 2000)


def test_duplicate_upload_does_not_store_a_second_file(service, owner, storage):
    first = service.create_from_upload(owner.id, "a.md", b"# T\nbody text")
    second = service.create_from_upload(owner.id, "b.md", b"# T\nbody text")
    assert first.created is True and second.created is False
    assert len([p for p in storage.root.rglob("*") if p.is_file()]) == 1


def test_path_like_filenames_are_sanitized(service, owner):
    source = service.create_from_upload(owner.id, "../../evil.md", b"# T\nbody text").source
    assert source.original_filename == "evil.md"
    assert source.storage_path.startswith(str(owner.id))


def test_replacing_a_file_reindexes_and_overwrites_it(service, owner, db_session, storage):
    source = service.create_from_upload(owner.id, "n.txt", b"first version of the text").source
    service.replace_file(owner.id, source.id, "n.txt", b"second version of the text")
    assert "second" in source.content_text
    assert all("first" not in c.content for c in chunks_of(db_session, source))
    assert storage.read(source.storage_path) == b"second version of the text"


def test_replacing_with_identical_content_changes_nothing(service, owner, db_session):
    source = service.create_from_upload(owner.id, "n.txt", b"same text here").source
    old_ids = {c.id for c in chunks_of(db_session, source)}
    result = service.replace_file(owner.id, source.id, "n.txt", b"same text here")
    assert result.created is False
    assert {c.id for c in chunks_of(db_session, source)} == old_ids


def test_replacement_must_be_the_same_kind(service, owner):
    source = service.create_from_upload(owner.id, "n.txt", b"some text here").source
    with pytest.raises(InvalidInputError):
        service.replace_file(owner.id, source.id, "x.pdf", b"%PDF-1.4")
    note = service.create_note(owner.id, "N", "a note body").source
    with pytest.raises(InvalidInputError):
        service.replace_file(owner.id, note.id, "n.txt", b"text")


# ================================================================== embeddings
def test_embedding_failure_does_not_fail_ingestion(make_service, owner, db_session):
    result = make_service(embedder=BrokenEmbedder()).create_note(owner.id, "N", RAG_TEXT)
    assert result.source.status == IngestionStatus.COMPLETED
    assert result.warnings
    chunks = chunks_of(db_session, result.source)
    assert chunks and all(c.embedding is None and c.embedding_model is None for c in chunks)


def test_ingestion_works_without_an_embedder(make_service, owner, db_session):
    result = make_service(embedder=None).create_note(owner.id, "N", RAG_TEXT)
    assert result.warnings == []
    assert all(c.embedding is None for c in chunks_of(db_session, result.source))


# =================================================================== bookmarks
def test_bookmark_is_queued_not_fetched(service, owner, db_session, fake_fetcher):
    result = service.create_bookmark(owner.id, "https://Example.com/a?utm_source=x#frag")
    source = result.source
    assert result.created is True
    assert source.status == IngestionStatus.PENDING
    assert source.canonical_url == "https://example.com/a"
    assert source.original_url == "https://Example.com/a?utm_source=x#frag"
    (job,) = db_session.scalars(select(Job)).all()
    assert job.job_type == JOB_INGEST_URL and job.status == JobStatus.PENDING
    assert job.source_id == source.id
    assert fake_fetcher.calls == []


def test_same_page_is_not_bookmarked_twice(service, owner, db_session):
    first = service.create_bookmark(owner.id, "https://example.com/a")
    second = service.create_bookmark(owner.id, "https://example.com/a?utm_medium=mail#top")
    assert second.created is False and second.source.id == first.source.id
    assert count(db_session, Job) == 1


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "ftp://example.com/x",
        "http://example.com:8080/",
    ],
)
def test_dangerous_bookmarks_are_refused_immediately(service, owner, db_session, url):
    with pytest.raises(UnsafeUrlError):
        service.create_bookmark(owner.id, url)
    assert count(db_session, Source) == 0


def test_processing_a_bookmark_indexes_the_page(service, owner, db_session, fake_fetcher):
    fake_fetcher.add_html("https://example.com/a", ARTICLE)
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.process_bookmark(owner.id, source.id)
    db_session.refresh(source)
    assert source.status == IngestionStatus.COMPLETED
    assert source.title == "My Article"
    assert source.fetched_at is not None and source.error_message is None
    assert chunks_of(db_session, source)


def test_refresh_with_unchanged_page_keeps_chunks_and_with_changed_page_replaces_them(
    service, owner, db_session, fake_fetcher
):
    url = "https://example.com/a"
    fake_fetcher.add_html(url, ARTICLE)
    source = service.create_bookmark(owner.id, url).source
    service.process_bookmark(owner.id, source.id)
    old_ids = {c.id for c in chunks_of(db_session, source)}

    service.refresh_bookmark(owner.id, source.id)
    assert source.status == IngestionStatus.PENDING
    assert count(db_session, Job) == 1  # the waiting job was reused
    service.process_bookmark(owner.id, source.id)
    assert {c.id for c in chunks_of(db_session, source)} == old_ids

    fake_fetcher.add_html(url, ARTICLE.replace("Retrieval", "Vectorized"))
    service.refresh_bookmark(owner.id, source.id)
    service.process_bookmark(owner.id, source.id)
    new_chunks = chunks_of(db_session, source)
    assert {c.id for c in new_chunks}.isdisjoint(old_ids)
    assert any("Vectorized" in c.content for c in new_chunks)


def test_canonical_url_from_another_site_is_ignored(service, owner, db_session, fake_fetcher):
    html = ARTICLE.replace("<head>", '<head><link rel="canonical" href="https://evil.test/x">')
    fake_fetcher.add_html("https://example.com/a", html)
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.process_bookmark(owner.id, source.id)
    db_session.refresh(source)
    assert source.canonical_url == "https://example.com/a"


def test_canonical_url_on_the_same_site_is_used(service, owner, db_session, fake_fetcher):
    html = ARTICLE.replace("<head>", '<head><link rel="canonical" href="/canonical-a">')
    fake_fetcher.add_html("https://example.com/a", html)
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.process_bookmark(owner.id, source.id)
    db_session.refresh(source)
    assert source.canonical_url == "https://example.com/canonical-a"


def test_two_bookmarks_with_the_same_content_are_not_both_indexed(
    service, owner, fake_fetcher
):
    fake_fetcher.add_html("https://example.com/a", ARTICLE)
    fake_fetcher.add_html("https://example.com/b", ARTICLE)
    first = service.create_bookmark(owner.id, "https://example.com/a").source
    second = service.create_bookmark(owner.id, "https://example.com/b").source
    service.process_bookmark(owner.id, first.id)
    with pytest.raises(IngestionError, match="already saved"):
        service.process_bookmark(owner.id, second.id)


def test_processing_a_deleted_bookmark_is_a_quiet_no_op(service, owner, fake_fetcher):
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.delete_source(owner.id, source.id)
    service.process_bookmark(owner.id, source.id)
    assert fake_fetcher.calls == []


def test_failure_is_recorded_on_the_bookmark(service, owner, db_session):
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.record_bookmark_failure(owner.id, source.id, "The site returned HTTP 404.", will_retry=False)
    db_session.refresh(source)
    assert source.status == IngestionStatus.FAILED
    assert source.error_message == "The site returned HTTP 404."
    service.record_bookmark_failure(owner.id, source.id, "Site slow", will_retry=True)
    db_session.refresh(source)
    assert source.status == IngestionStatus.PENDING and "Retrying" in source.error_message


# ============================================================== delete / isolation
def test_deleting_a_source_removes_derived_data_and_the_file(service, owner, db_session, storage):
    source = service.create_from_upload(owner.id, "a.md", b"# T\nbody text", tags=["x"]).source
    path = source.storage_path
    service.delete_source(owner.id, source.id)
    assert count(db_session, DocumentChunk) == 0
    assert count(db_session, SourceTag) == 0
    assert count(db_session, Tag) == 1  # the tag belongs to the user, not the source
    with pytest.raises(FileNotFoundError):
        storage.read(path)
    with pytest.raises(NotFoundError):
        service.get_source(owner.id, source.id)


def test_deleting_a_bookmark_removes_its_queued_job(service, owner, db_session):
    source = service.create_bookmark(owner.id, "https://example.com/a").source
    service.delete_source(owner.id, source.id)
    assert count(db_session, Job) == 0


def test_other_users_cannot_see_or_change_a_source(service, owner, other_user):
    source = service.create_note(owner.id, "Private", RAG_TEXT).source
    bookmark = service.create_bookmark(owner.id, "https://example.com/a").source
    with pytest.raises(NotFoundError):
        service.get_source(other_user.id, source.id)
    with pytest.raises(NotFoundError):
        service.delete_source(other_user.id, source.id)
    with pytest.raises(NotFoundError):
        service.update_source(other_user.id, source.id, title="x")
    with pytest.raises(NotFoundError):
        service.refresh_bookmark(other_user.id, bookmark.id)
    with pytest.raises(NotFoundError):
        service.list_chunks(other_user.id, source.id)
    assert service.list_sources(other_user.id) == ([], 0)


def test_listing_filters_and_paginates(service, owner):
    service.create_note(owner.id, "One", "alpha one", tags=["a"])
    service.create_note(owner.id, "Two", "beta two", tags=["b"])
    service.create_from_upload(owner.id, "x.md", b"# X\ntext here")
    service.create_bookmark(owner.id, "https://example.com/z")
    assert service.list_sources(owner.id)[1] == 4
    notes, _ = service.list_sources(owner.id, source_type=SourceType.NOTE)
    assert len(notes) == 2
    assert len(service.list_sources(owner.id, tag="A")[0]) == 1
    assert len(service.list_sources(owner.id, status=IngestionStatus.PENDING)[0]) == 1
    page_one, total = service.list_sources(owner.id, limit=2, offset=0)
    page_two, _ = service.list_sources(owner.id, limit=2, offset=2)
    assert total == 4 and len(page_one) == 2 and len(page_two) == 2
    assert {s.id for s in page_one}.isdisjoint({s.id for s in page_two})