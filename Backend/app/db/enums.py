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
    
class MemoryType(str, enum.Enum):
    PREFERENCE = "preference"
    GOAL = "goal"
    PROJECT = "project"
    FACT = "fact"
    INSTRUCTION = "instruction"
    CORRECTION = "correction"
    TEMPORARY = "temporary"
    HYPOTHESIS = "hypothesis"  # system guess awaiting user confirmation


class MemoryStatus(str, enum.Enum):
    ACTIVE = "active"
    CANDIDATE = "candidate"  # proposed, not yet confirmed by the user
    SUPERSEDED = "superseded"  # replaced by a newer memory
    REJECTED = "rejected"
    ARCHIVED = "archived"


class MemoryVerification(str, enum.Enum):
    USER_CONFIRMED = "user_confirmed"
    USER_STATED = "user_stated"
    SYSTEM_INFERRED = "system_inferred"  # never treated as a verified user fact


class MemoryActor(str, enum.Enum):
    USER = "user"
    SYSTEM = "system"


class MemoryAction(str, enum.Enum):
    CREATED = "created"
    UPDATED = "updated"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    RESTORED = "restored"


class EvidenceType(str, enum.Enum):
    SOURCE = "source"
    CHUNK = "chunk"
    USER_STATEMENT = "user_statement"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class AnswerStatus(str, enum.Enum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"


class NodeType(str, enum.Enum):
    SOURCE = "source"
    MEMORY = "memory"
    CONCEPT = "concept"


class RelationType(str, enum.Enum):
    RELATED_TO = "related_to"  # topical connection
    SUPPORTS = "supports"  # A provides evidence for B
    CONTRADICTS = "contradicts"  # A conflicts with B
    EXTENDS = "extends"  # A builds on B
    REFERENCES = "references"  # A explicitly mentions B
    SUPERSEDES = "supersedes"  # A replaces B


class RelationOrigin(str, enum.Enum):
    TEXTUAL_REFERENCE = "textual_reference"  # found directly in the text
    SEMANTIC_SIMILARITY = "semantic_similarity"  # embedding closeness only
    USER_CREATED = "user_created"
    LLM_SUGGESTED = "llm_suggested"


class RelationStatus(str, enum.Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"