"""Plain data containers passed between ingestion stages."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Section:
    """A piece of a document that shares one page number and/or heading."""

    text: str
    page_number: int | None = None
    heading: str | None = None


@dataclass(frozen=True)
class ParsedDocument:
    """What a parser returns: raw sections plus metadata. Not yet normalized."""

    sections: list[Section]
    title: str | None = None
    page_count: int | None = None
    canonical_url: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlacedSection:
    """A normalized section and where it starts inside the whole document text."""

    text: str
    page_number: int | None
    heading: str | None
    start: int


@dataclass(frozen=True)
class TextChunk:
    sequence: int
    content: str
    page_number: int | None
    section_heading: str | None
    char_start: int  # offsets into the document's content_text
    char_end: int
    content_hash: str