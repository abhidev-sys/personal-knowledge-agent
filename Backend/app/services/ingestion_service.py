"""Ingestion service: turns notes, uploaded files and bookmarked URLs into indexed sources.

Every public method commits its own transaction, so a failure never leaves half a source.
"""
import ipaddress
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    IngestionError,
    InvalidInputError,
    NotFoundError,
    PayloadTooLargeError,
    TransientIngestionError,
    UnsafeUrlError,
)
from app.db.enums import IngestionStatus, SourceType
from app.db.models import DocumentChunk, Source, SourceTag, Tag
from app.db.types import utcnow
from app.ingestion.chunking import assemble_document, chunk_document
from app.ingestion.fetcher import FetchedPage
from app.ingestion.parsers import parse_html, parse_markdown, parse_pdf, parse_plain_text
from app.ingestion.storage import FileStorage
from app.ingestion.text import clean_title, content_hash, decode_text_bytes, normalize_text
from app.ingestion.types import ParsedDocument, TextChunk
from app.ingestion.url_safety import is_public_ip, validate_url
from app.jobs.queue import JOB_INGEST_URL, enqueue
from app.providers.embeddings import EmbeddingProvider, embed_in_batches

log = logging.getLogger(__name__)

MAX_TAGS = 20
MAX_TAG_LENGTH = 64

UPLOAD_TYPES = {
    ".pdf": SourceType.PDF,
    ".md": SourceType.MARKDOWN,
    ".markdown": SourceType.MARKDOWN,
    ".txt": SourceType.TEXT,
}
STORED_SUFFIX = {SourceType.PDF: ".pdf", SourceType.MARKDOWN: ".md", SourceType.TEXT: ".txt"}
MIME_TYPES = {
    SourceType.PDF: "application/pdf",
    SourceType.MARKDOWN: "text/markdown",
    SourceType.TEXT: "text/plain",
}
EMBEDDING_WARNING = (
    "Embeddings could not be created; this is searchable by keywords only until it is re-embedded."
)


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


@dataclass(frozen=True)
class IngestResult:
    source: Source
    created: bool  # False = an identical source already existed and was returned instead
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceSummary:
    id: uuid.UUID
    source_type: SourceType
    title: str
    status: IngestionStatus
    original_url: str | None
    canonical_url: str | None
    original_filename: str | None
    mime_type: str | None
    size_bytes: int | None
    page_count: int | None
    error_message: str | None
    tags: list[str]
    chunk_count: int
    created_at: datetime
    updated_at: datetime
    fetched_at: datetime | None
    last_synced_at: datetime | None


@dataclass(frozen=True)
class _Prepared:
    text: str
    chunks: list[TextChunk]
    digest: str


# ---------------------------------------------------------------------- pure helpers
def normalize_tags(tags: Sequence[str] | None) -> list[str]:
    """Lowercase, trim, drop blanks and duplicates (order kept)."""
    result: list[str] = []
    for raw in tags or []:
        name = " ".join(normalize_text(raw).split()).lower()
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise InvalidInputError(f"A tag is longer than {MAX_TAG_LENGTH} characters.")
        if name not in result:
            result.append(name)
    if len(result) > MAX_TAGS:
        raise InvalidInputError(f"At most {MAX_TAGS} tags are allowed.")
    return result


def safe_filename(raw: str | None) -> str:
    """Keep only the last path part and printable characters. Never used to build a path."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in normalize_text(name) if ch.isprintable() and ch not in '<>:"|?*')
    return name.strip(" .")[:255] or "upload"


def _reject_private_ip_literal(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a name: its DNS answer is checked when the page is fetched
    if not is_public_ip(ip):
        raise UnsafeUrlError("The URL points to a private or internal address, which is not allowed.")


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _choose_canonical(final_url: str, declared: str | None) -> str:
    """Trust a page's canonical URL only when it stays on the same site."""
    if declared:
        try:
            candidate = validate_url(declared)
            final = validate_url(final_url)
        except IngestionError:
            return final_url
        if _bare_host(candidate.host) == _bare_host(final.host):
            return candidate.normalized
    return final_url


class IngestionService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        storage: FileStorage,
        embedder: EmbeddingProvider | None = None,
        fetcher: PageFetcher | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.storage = storage
        self.embedder = embedder
        self.fetcher = fetcher

    # ================================================================== notes
    def create_note(
        self, owner_id: uuid.UUID, title: str | None, content: str, tags: Sequence[str] | None = None
    ) -> IngestResult:
        tag_names = normalize_tags(tags)
        text = normalize_text(content or "")
        if not text:
            raise InvalidInputError("The note is empty.")
        note_title = clean_title(
            title, fallback=text.split("\n", 1)[0][:80] or "Untitled note"
        )
        prepared = self._prepare(parse_plain_text(text, note_title), hash_title=note_title)
        return self._create_indexed(
            owner_id=owner_id,
            source_type=SourceType.NOTE,
            title=note_title,
            prepared=prepared,
            tags=tag_names,
            fields={"size_bytes": len(prepared.text.encode("utf-8"))},
        )

    def update_source(
        self,
        owner_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> Source:
        """Edit a note (title/content, re-indexed) and/or set the tags of any source."""
        source = self.get_source(owner_id, source_id)
        tag_names = normalize_tags(tags) if tags is not None else None
        edits_note = title is not None or content is not None
        if edits_note and source.source_type is not SourceType.NOTE:
            raise InvalidInputError("Only notes can be edited. Upload a replacement file instead.")
        try:
            if edits_note:
                self._edit_note(source, title, content)
            if tag_names is not None:
                self._set_tags(source, tag_names)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("The change conflicts with another saved item; try again.") from exc
        except Exception:
            self.session.rollback()
            raise
        return source

    def _edit_note(self, source: Source, title: str | None, content: str | None) -> None:
        new_title = clean_title(title if title is not None else source.title, fallback=source.title)
        new_text = normalize_text(content) if content is not None else (source.content_text or "")
        if not new_text:
            raise InvalidInputError("The note is empty.")
        prepared = self._prepare(parse_plain_text(new_text, new_title), hash_title=new_title)
        if prepared.digest == source.content_hash:
            return
        other = self._find_by_hash(source.owner_id, SourceType.NOTE, prepared.digest)
        if other is not None and other.id != source.id:
            raise ConflictError("Another note has the same title and content.")
        text_changed = prepared.text != source.content_text
        source.title = new_title
        source.content_text = prepared.text
        source.content_hash = prepared.digest
        source.size_bytes = len(prepared.text.encode("utf-8"))
        source.status = IngestionStatus.COMPLETED
        source.error_message = None
        source.last_synced_at = utcnow()
        if text_changed:  # a title-only change keeps the existing chunks and embeddings
            vectors, _ = self._embed(prepared.chunks)
            self._write_chunks(source, prepared.chunks, vectors)

    # ============================================================ uploaded files
    def create_from_upload(
        self,
        owner_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        tags: Sequence[str] | None = None,
    ) -> IngestResult:
        tag_names = normalize_tags(tags)
        name, kind = self._check_upload(filename, data)
        document, title = self._parse_upload(kind, name, data)
        prepared = self._prepare(document, hash_title=None)
        return self._create_indexed(
            owner_id=owner_id,
            source_type=kind,
            title=title,
            prepared=prepared,
            tags=tag_names,
            fields={
                "original_filename": name,
                "mime_type": MIME_TYPES[kind],
                "size_bytes": len(data),
                "page_count": document.page_count,
            },
            warnings=document.warnings,
            file_bytes=data,
        )

    def replace_file(
        self, owner_id: uuid.UUID, source_id: uuid.UUID, filename: str | None, data: bytes
    ) -> IngestResult:
        """Swap an uploaded file for a new version of the same kind and re-index it."""
        source = self.get_source(owner_id, source_id)
        if source.source_type not in STORED_SUFFIX:
            raise InvalidInputError("Only uploaded files can be replaced.")
        name, kind = self._check_upload(filename, data)
        if kind is not source.source_type:
            raise InvalidInputError("The replacement must be the same kind of file as the original.")
        document, title = self._parse_upload(kind, name, data)
        prepared = self._prepare(document, hash_title=None)
        if prepared.digest == source.content_hash:
            return IngestResult(source, False, list(document.warnings))
        other = self._find_by_hash(owner_id, kind, prepared.digest)
        if other is not None and other.id != source.id:
            raise ConflictError("Another saved file already has this content.")
        vectors, warnings = self._embed(prepared.chunks)
        try:
            source.storage_path = self.storage.save(owner_id, source.id, data, STORED_SUFFIX[kind])
            source.title = title
            source.original_filename = name
            source.mime_type = MIME_TYPES[kind]
            source.size_bytes = len(data)
            source.page_count = document.page_count
            source.content_text = prepared.text
            source.content_hash = prepared.digest
            source.status = IngestionStatus.COMPLETED
            source.error_message = None
            source.last_synced_at = utcnow()
            self._write_chunks(source, prepared.chunks, vectors)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("The replacement conflicts with another saved item.") from exc
        except Exception:
            self.session.rollback()
            raise
        return IngestResult(source, False, list(document.warnings) + warnings)

    def _check_upload(self, filename: str | None, data: bytes) -> tuple[str, SourceType]:
        name = safe_filename(filename)
        kind = UPLOAD_TYPES.get(Path(name).suffix.lower())
        if kind is None:
            raise InvalidInputError("Unsupported file type. Allowed: .pdf, .md, .markdown, .txt")
        if not data:
            raise InvalidInputError("The file is empty.")
        if len(data) > self.settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"The file is larger than the {self.settings.max_upload_bytes:,} byte limit."
            )
        return name, kind

    def _parse_upload(
        self, kind: SourceType, name: str, data: bytes
    ) -> tuple[ParsedDocument, str]:
        if kind is SourceType.PDF:
            if b"%PDF-" not in data[:1024]:
                raise IngestionError("This file is not a valid PDF.")
            document = parse_pdf(data, max_pages=self.settings.max_pdf_pages)
        elif kind is SourceType.MARKDOWN:
            document = parse_markdown(decode_text_bytes(data))
        else:
            document = parse_plain_text(decode_text_bytes(data))
        return document, clean_title(document.title, fallback=Path(name).stem or name)

    # ================================================================ bookmarks
    def create_bookmark(
        self, owner_id: uuid.UUID, url: str, tags: Sequence[str] | None = None
    ) -> IngestResult:
        """Save a URL and queue it for fetching. Nothing is downloaded in this call."""
        tag_names = normalize_tags(tags)
        validated = validate_url(url)
        _reject_private_ip_literal(validated.host)
        canonical = validated.normalized
        existing = self._find_by_canonical(owner_id, canonical)
        if existing is not None:
            return IngestResult(existing, False)
        try:
            source = Source(
                owner_id=owner_id,
                source_type=SourceType.BOOKMARK,
                title=validated.host[:500],
                original_url=url.strip(),
                canonical_url=canonical,
                status=IngestionStatus.PENDING,
            )
            self.session.add(source)
            self.session.flush()
            self._set_tags(source, tag_names)
            enqueue(
                self.session,
                owner_id=owner_id,
                source_id=source.id,
                job_type=JOB_INGEST_URL,
                max_attempts=self.settings.job_max_attempts,
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self._find_by_canonical(owner_id, canonical)
            if existing is not None:
                return IngestResult(existing, False)
            raise ConflictError("The bookmark could not be saved; try again.") from exc
        except Exception:
            self.session.rollback()
            raise
        return IngestResult(source, True)

    def refresh_bookmark(self, owner_id: uuid.UUID, source_id: uuid.UUID) -> Source:
        source = self.get_source(owner_id, source_id)
        if source.source_type is not SourceType.BOOKMARK:
            raise InvalidInputError("Only bookmarks can be refreshed.")
        source.status = IngestionStatus.PENDING
        source.error_message = None
        enqueue(
            self.session,
            owner_id=owner_id,
            source_id=source.id,
            job_type=JOB_INGEST_URL,
            max_attempts=self.settings.job_max_attempts,
        )
        self.session.commit()
        return source

    def process_bookmark(self, owner_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Worker entry point: fetch, parse and index a bookmarked page.

        Raises TransientIngestionError (worth retrying) or IngestionError (permanent).
        """
        if self.fetcher is None:
            raise RuntimeError("This service instance has no fetcher.")
        source = self._find_owned(owner_id, source_id)
        if source is None or source.source_type is not SourceType.BOOKMARK:
            return  # deleted while the job was waiting
        url = source.original_url
        if not url:
            raise IngestionError("The bookmark has no URL.")
        source.status = IngestionStatus.PROCESSING
        source.error_message = None
        self.session.commit()  # visible to the user; no transaction stays open during the download

        page = self.fetcher.fetch(url)
        if page.content_type == "text/plain":
            document = parse_plain_text(decode_text_bytes(page.content))
        else:
            document = parse_html(page.content, page_url=page.final_url)
        prepared = self._prepare(document, hash_title=None)
        canonical = _choose_canonical(page.final_url, document.canonical_url)
        title = clean_title(document.title, fallback=canonical)

        source = self._find_owned(owner_id, source_id, refresh=True)
        if source is None:
            return  # deleted during the download
        clash = self.session.scalar(
            select(Source)
            .where(
                Source.owner_id == owner_id,
                Source.source_type == SourceType.BOOKMARK,
                Source.id != source.id,
                or_(Source.canonical_url == canonical, Source.content_hash == prepared.digest),
            )
            .limit(1)
        )
        if clash is not None:
            raise IngestionError(f'This page is already saved as "{clash.title}".')

        try:
            unchanged = source.content_hash == prepared.digest and self._chunk_count(source.id) > 0
            source.fetched_at = page.fetched_at
            source.last_synced_at = utcnow()
            source.status = IngestionStatus.COMPLETED
            source.error_message = None
            if not unchanged:
                vectors, _ = self._embed(prepared.chunks)
                source.title = title
                source.canonical_url = canonical
                source.content_text = prepared.text
                source.content_hash = prepared.digest
                source.size_bytes = len(page.content)
                self._write_chunks(source, prepared.chunks, vectors)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise TransientIngestionError(
                "The page could not be saved because of a concurrent change."
            ) from exc

    def record_bookmark_failure(
        self, owner_id: uuid.UUID, source_id: uuid.UUID, message: str, *, will_retry: bool
    ) -> None:
        source = self._find_owned(owner_id, source_id)
        if source is None or source.source_type is not SourceType.BOOKMARK:
            return
        if will_retry:
            source.status = IngestionStatus.PENDING
            source.error_message = f"Retrying after an error: {message}"
        else:
            source.status = IngestionStatus.FAILED
            source.error_message = message
        self.session.commit()

    # ============================================================ read / delete
    def get_source(self, owner_id: uuid.UUID, source_id: uuid.UUID) -> Source:
        source = self._find_owned(owner_id, source_id)
        if source is None:  # also what another user's source looks like: no existence leak
            raise NotFoundError("Source not found.")
        return source

    def list_sources(
        self,
        owner_id: uuid.UUID,
        *,
        source_type: SourceType | None = None,
        status: IngestionStatus | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Source], int]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        query = select(Source).where(Source.owner_id == owner_id)
        if source_type is not None:
            query = query.where(Source.source_type == source_type)
        if status is not None:
            query = query.where(Source.status == status)
        if tag:
            names = normalize_tags([tag])
            if names:
                tagged = (
                    select(SourceTag.source_id)
                    .join(Tag, Tag.id == SourceTag.tag_id)
                    .where(Tag.owner_id == owner_id, Tag.name == names[0])
                )
                query = query.where(Source.id.in_(tagged))
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        items = self.session.scalars(
            query.order_by(Source.created_at.desc(), Source.id).offset(offset).limit(limit)
        ).all()
        return list(items), total

    def list_chunks(
        self, owner_id: uuid.UUID, source_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DocumentChunk]:
        self.get_source(owner_id, source_id)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        return list(
            self.session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.source_id == source_id, DocumentChunk.owner_id == owner_id)
                .order_by(DocumentChunk.sequence)
                .offset(offset)
                .limit(limit)
            )
        )

    def summaries(self, sources: Sequence[Source]) -> list[SourceSummary]:
        if not sources:
            return []
        ids = [s.id for s in sources]
        tag_rows = self.session.execute(
            select(SourceTag.source_id, Tag.name)
            .join(Tag, Tag.id == SourceTag.tag_id)
            .where(SourceTag.source_id.in_(ids))
            .order_by(Tag.name)
        ).all()
        tags: dict[uuid.UUID, list[str]] = {}
        for source_id, name in tag_rows:
            tags.setdefault(source_id, []).append(name)
        count_rows = self.session.execute(
            select(DocumentChunk.source_id, func.count())
            .where(DocumentChunk.source_id.in_(ids))
            .group_by(DocumentChunk.source_id)
        ).all()
        counts = {source_id: count for source_id, count in count_rows}
        return [
            SourceSummary(
                id=s.id,
                source_type=s.source_type,
                title=s.title,
                status=s.status,
                original_url=s.original_url,
                canonical_url=s.canonical_url,
                original_filename=s.original_filename,
                mime_type=s.mime_type,
                size_bytes=s.size_bytes,
                page_count=s.page_count,
                error_message=s.error_message,
                tags=tags.get(s.id, []),
                chunk_count=counts.get(s.id, 0),
                created_at=s.created_at,
                updated_at=s.updated_at,
                fetched_at=s.fetched_at,
                last_synced_at=s.last_synced_at,
            )
            for s in sources
        ]

    def delete_source(self, owner_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Delete a source. The database cascades to chunks, tag links, jobs, citations, graph edges."""
        source = self.get_source(owner_id, source_id)
        storage_path = source.storage_path
        self.session.delete(source)
        self.session.commit()
        self._discard_file(storage_path)

    # ================================================================= internals
    def _find_owned(
        self, owner_id: uuid.UUID, source_id: uuid.UUID, *, refresh: bool = False
    ) -> Source | None:
        query = select(Source).where(Source.id == source_id, Source.owner_id == owner_id)
        if refresh:
            query = query.execution_options(populate_existing=True)
        return self.session.scalar(query)

    def _find_by_hash(
        self, owner_id: uuid.UUID, source_type: SourceType, digest: str
    ) -> Source | None:
        return self.session.scalar(
            select(Source)
            .where(
                Source.owner_id == owner_id,
                Source.source_type == source_type,
                Source.content_hash == digest,
            )
            .limit(1)
        )

    def _find_by_canonical(self, owner_id: uuid.UUID, canonical: str) -> Source | None:
        return self.session.scalar(
            select(Source)
            .where(Source.owner_id == owner_id, Source.canonical_url == canonical)
            .limit(1)
        )

    def _chunk_count(self, source_id: uuid.UUID) -> int:
        return (
            self.session.scalar(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.source_id == source_id)
            )
            or 0
        )

    def _prepare(self, document: ParsedDocument, *, hash_title: str | None) -> _Prepared:
        text, placed = assemble_document(document.sections)
        if not text:
            raise IngestionError("There is no readable text in this content.")
        if len(text) > self.settings.max_text_chars:
            raise PayloadTooLargeError(
                f"The text is too long ({len(text):,} characters; the limit is "
                f"{self.settings.max_text_chars:,})."
            )
        chunks = chunk_document(
            placed, size=self.settings.chunk_size_chars, overlap=self.settings.chunk_overlap_chars
        )
        return _Prepared(text, chunks, content_hash(text, title=hash_title))

    def _embed(self, chunks: list[TextChunk]) -> tuple[list[list[float]] | None, list[str]]:
        """Embedding trouble never fails ingestion: the chunks are stored without vectors."""
        if self.embedder is None or not chunks:
            return None, []
        try:
            vectors = embed_in_batches(
                self.embedder, [c.content for c in chunks], self.settings.embedding_batch_size
            )
        except Exception as exc:
            log.warning("embedding_failed", extra={"ctx": {"error_type": type(exc).__name__}})
            return None, [EMBEDDING_WARNING]
        return vectors, []

    def _write_chunks(
        self, source: Source, chunks: list[TextChunk], vectors: list[list[float]] | None
    ) -> None:
        """Replace ALL chunks of a source (old text must never stay searchable)."""
        self.session.execute(delete(DocumentChunk).where(DocumentChunk.source_id == source.id))
        self.session.expire(source, ["chunks"])
        model_id = self.embedder.model_id if (vectors is not None and self.embedder) else None
        for index, chunk in enumerate(chunks):
            self.session.add(
                DocumentChunk(
                    source_id=source.id,
                    owner_id=source.owner_id,
                    sequence=chunk.sequence,
                    content=chunk.content,
                    page_number=chunk.page_number,
                    section_heading=chunk.section_heading,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    content_hash=chunk.content_hash,
                    embedding_model=model_id,
                    embedding=vectors[index] if vectors is not None else None,
                )
            )
        self.session.flush()

    def _set_tags(self, source: Source, names: list[str]) -> None:
        self.session.execute(delete(SourceTag).where(SourceTag.source_id == source.id))
        existing: dict[str, Tag] = {}
        if names:
            existing = {
                tag.name: tag
                for tag in self.session.scalars(
                    select(Tag).where(Tag.owner_id == source.owner_id, Tag.name.in_(names))
                )
            }
        for name in names:
            tag = existing.get(name)
            if tag is None:
                tag = Tag(owner_id=source.owner_id, name=name)
                self.session.add(tag)
                self.session.flush()
            self.session.add(SourceTag(source_id=source.id, tag_id=tag.id, owner_id=source.owner_id))
        self.session.flush()

    def _create_indexed(
        self,
        *,
        owner_id: uuid.UUID,
        source_type: SourceType,
        title: str,
        prepared: _Prepared,
        tags: list[str],
        fields: dict[str, object],
        warnings: Sequence[str] = (),
        file_bytes: bytes | None = None,
    ) -> IngestResult:
        existing = self._find_by_hash(owner_id, source_type, prepared.digest)
        if existing is not None:
            return IngestResult(existing, False)
        vectors, embed_warnings = self._embed(prepared.chunks)
        source_id = uuid.uuid4()
        storage_path: str | None = None
        try:
            if file_bytes is not None:
                storage_path = self.storage.save(
                    owner_id, source_id, file_bytes, STORED_SUFFIX[source_type]
                )
            source = Source(
                id=source_id,
                owner_id=owner_id,
                source_type=source_type,
                title=title,
                content_text=prepared.text,
                content_hash=prepared.digest,
                status=IngestionStatus.COMPLETED,
                last_synced_at=utcnow(),
                storage_path=storage_path,
                **fields,
            )
            self.session.add(source)
            self.session.flush()
            self._write_chunks(source, prepared.chunks, vectors)
            self._set_tags(source, tags)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            self._discard_file(storage_path)
            existing = self._find_by_hash(owner_id, source_type, prepared.digest)
            if existing is not None:  # lost a race against an identical request
                return IngestResult(existing, False)
            raise ConflictError("The item could not be saved because of a conflict; try again.") from exc
        except Exception:
            self.session.rollback()
            self._discard_file(storage_path)
            raise
        return IngestResult(source, True, list(warnings) + embed_warnings)

    def _discard_file(self, storage_path: str | None) -> None:
        if not storage_path:
            return
        try:
            self.storage.delete(storage_path)
        except OSError:
            log.warning("file_cleanup_failed")  # path is not logged: it contains user ids