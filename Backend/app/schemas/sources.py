"""Request and response models for the sources API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import IngestionStatus, SourceType


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are errors, not silently ignored


class NoteCreate(_Strict):
    title: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1)  # the real size limit is enforced by limits.py and the service
    tags: list[str] = Field(default_factory=list, max_length=20)


class BookmarkCreate(_Strict):
    url: str = Field(min_length=1, max_length=2048)
    tags: list[str] = Field(default_factory=list, max_length=20)


class SourceUpdate(_Strict):
    """Edit a note (title, content) and/or set the tags of any source. Send at least one field."""

    title: str | None = Field(default=None, max_length=500)
    content: str | None = None
    tags: list[str] | None = Field(default=None, max_length=20)  # [] removes all tags

    @model_validator(mode="after")
    def _needs_a_change(self) -> "SourceUpdate":
        if not self.model_fields_set & {"title", "content", "tags"}:
            raise ValueError("Send at least one of: title, content, tags.")
        return self


class SourceOut(BaseModel):
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


class SourceDetail(SourceOut):
    content_preview: str | None  # first 2,000 characters; the full text is at /content
    has_file: bool  # an original uploaded file is stored (download at /file)


class IngestResponse(BaseModel):
    source: SourceOut
    created: bool  # False = an identical item already existed and was returned
    warnings: list[str]


class SourceList(BaseModel):
    items: list[SourceOut]
    total: int
    limit: int
    offset: int


class SourceContent(BaseModel):
    id: uuid.UUID
    content: str


class ChunkOut(BaseModel):
    id: uuid.UUID
    sequence: int
    content: str
    page_number: int | None
    section_heading: str | None
    char_start: int | None
    char_end: int | None
    embedding_model: str | None
    has_embedding: bool  # vectors themselves are never sent to clients