"""Closed sets of values stored in the database as strings (validated by CHECK constraints)."""
import enum


class SourceType(str, enum.Enum):
    NOTE = "note"
    PDF = "pdf"
    BOOKMARK = "bookmark"
    MARKDOWN = "markdown"
    TEXT = "text"


class IngestionStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class MemoryMode(str, enum.Enum):
    """How the agent may create memories automatically."""

    OFF = "off"  # never create memories automatically
    SUGGEST = "suggest"  # create candidates that the user must confirm
    AUTO = "auto"  # save clearly stated facts directly (still auditable)