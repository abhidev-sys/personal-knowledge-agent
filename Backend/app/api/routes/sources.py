"""HTTP API for saved sources: notes, uploaded files and bookmarks."""
import uuid
from dataclasses import asdict
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from app.api.dependencies import get_current_user, get_ingestion_service, get_settings_dep
from app.api.limits import BodyLimitedRoute
from app.core.config import Settings
from app.core.exceptions import NotFoundError, PayloadTooLargeError
from app.db.enums import IngestionStatus, SourceType
from app.db.models import DocumentChunk, Source, User
from app.schemas.sources import (
    BookmarkCreate,
    ChunkOut,
    IngestResponse,
    NoteCreate,
    SourceContent,
    SourceDetail,
    SourceList,
    SourceOut,
    SourceUpdate,
)
from app.services.ingestion_service import IngestResult, IngestionService

router = APIRouter(prefix="/api/v1/sources", tags=["sources"], route_class=BodyLimitedRoute)

UserDep = Annotated[User, Depends(get_current_user)]
ServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

PREVIEW_CHARS = 2000


# ------------------------------------------------------------------------- helpers
def _out(service: IngestionService, source: Source) -> SourceOut:
    (summary,) = service.summaries([source])
    return SourceOut(**asdict(summary))


def _detail(service: IngestionService, source: Source) -> SourceDetail:
    (summary,) = service.summaries([source])
    preview = (source.content_text or "")[:PREVIEW_CHARS] or None
    return SourceDetail(
        **asdict(summary), content_preview=preview, has_file=bool(source.storage_path)
    )


def _result(service: IngestionService, result: IngestResult) -> IngestResponse:
    return IngestResponse(
        source=_out(service, result.source), created=result.created, warnings=list(result.warnings)
    )


def _chunk(chunk: DocumentChunk) -> ChunkOut:
    return ChunkOut(
        id=chunk.id,
        sequence=chunk.sequence,
        content=chunk.content,
        page_number=chunk.page_number,
        section_heading=chunk.section_heading,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        embedding_model=chunk.embedding_model,
        has_embedding=chunk.embedding is not None,
    )


def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    data = file.file.read(settings.max_upload_bytes + 1)  # never more than limit + 1 byte
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"The file is larger than the {settings.max_upload_bytes:,} byte limit."
        )
    return data


# --------------------------------------------------------------------------- create
@router.post("/notes", response_model=IngestResponse, status_code=201)
def create_note(
    body: NoteCreate, response: Response, user: UserDep, service: ServiceDep
) -> IngestResponse:
    """Save a note. An identical note already saved is returned with created=false (HTTP 200)."""
    result = service.create_note(user.id, body.title, body.content, body.tags)
    if not result.created:
        response.status_code = 200
    return _result(service, result)


@router.post("/upload", response_model=IngestResponse, status_code=201)
def upload_file(
    file: Annotated[UploadFile, File()],
    response: Response,
    user: UserDep,
    service: ServiceDep,
    settings: SettingsDep,
    tags: Annotated[list[str] | None, Form()] = None,  # repeat the form field for several tags
) -> IngestResponse:
    """Upload a PDF, Markdown or text file."""
    data = _read_upload(file, settings)
    result = service.create_from_upload(user.id, file.filename, data, tags)
    if not result.created:
        response.status_code = 200
    return _result(service, result)


@router.post("/bookmarks", response_model=IngestResponse, status_code=202)
def create_bookmark(
    body: BookmarkCreate, response: Response, user: UserDep, service: ServiceDep
) -> IngestResponse:
    """Save a URL. It is only queued (HTTP 202): a background worker downloads and indexes it."""
    result = service.create_bookmark(user.id, body.url, body.tags)
    if not result.created:
        response.status_code = 200
    return _result(service, result)


# ----------------------------------------------------------------------------- read
@router.get("", response_model=SourceList)
def list_sources(
    user: UserDep,
    service: ServiceDep,
    source_type: Annotated[SourceType | None, Query(alias="type")] = None,
    status: Annotated[IngestionStatus | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SourceList:
    sources, total = service.list_sources(
        user.id, source_type=source_type, status=status, tag=tag, limit=limit, offset=offset
    )
    items = [SourceOut(**asdict(summary)) for summary in service.summaries(sources)]
    return SourceList(items=items, total=total, limit=limit, offset=offset)


@router.get("/{source_id}", response_model=SourceDetail)
def get_source(source_id: uuid.UUID, user: UserDep, service: ServiceDep) -> SourceDetail:
    return _detail(service, service.get_source(user.id, source_id))


@router.get("/{source_id}/content", response_model=SourceContent)
def get_content(source_id: uuid.UUID, user: UserDep, service: ServiceDep) -> SourceContent:
    """The full normalized text (what the chunks and their offsets point into)."""
    source = service.get_source(user.id, source_id)
    return SourceContent(id=source.id, content=source.content_text or "")


@router.get("/{source_id}/chunks", response_model=list[ChunkOut])
def get_chunks(
    source_id: uuid.UUID,
    user: UserDep,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ChunkOut]:
    chunks = service.list_chunks(user.id, source_id, limit=limit, offset=offset)
    return [_chunk(chunk) for chunk in chunks]


@router.get("/{source_id}/file")
def download_file(source_id: uuid.UUID, user: UserDep, service: ServiceDep) -> Response:
    """Download the original uploaded file (always as an attachment, never rendered)."""
    source = service.get_source(user.id, source_id)
    if not source.storage_path:
        raise NotFoundError("This source has no stored file.")
    try:
        data = service.storage.read(source.storage_path)
    except FileNotFoundError as exc:
        raise NotFoundError("The stored file is missing.") from exc
    name = source.original_filename or "download"
    return Response(
        content=data,
        media_type=source.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------- change
@router.patch("/{source_id}", response_model=SourceDetail)
def update_source(
    source_id: uuid.UUID, body: SourceUpdate, user: UserDep, service: ServiceDep
) -> SourceDetail:
    """Edit a note (title/content: it is re-indexed) and/or set the tags of any source."""
    source = service.update_source(
        user.id, source_id, title=body.title, content=body.content, tags=body.tags
    )
    return _detail(service, source)


@router.put("/{source_id}/file", response_model=IngestResponse)
def replace_file(
    source_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    user: UserDep,
    service: ServiceDep,
    settings: SettingsDep,
) -> IngestResponse:
    """Replace an uploaded file with a new version of the same kind (re-indexed)."""
    data = _read_upload(file, settings)
    result = service.replace_file(user.id, source_id, file.filename, data)
    return _result(service, result)


@router.post("/{source_id}/refresh", response_model=SourceOut, status_code=202)
def refresh_bookmark(source_id: uuid.UUID, user: UserDep, service: ServiceDep) -> SourceOut:
    """Queue a bookmark to be downloaded and indexed again."""
    return _out(service, service.refresh_bookmark(user.id, source_id))


@router.delete("/{source_id}", status_code=204, response_class=Response)
def delete_source(source_id: uuid.UUID, user: UserDep, service: ServiceDep) -> Response:
    """Delete a source with its chunks, embeddings, tag links, queued jobs and original file."""
    service.delete_source(user.id, source_id)
    return Response(status_code=204)