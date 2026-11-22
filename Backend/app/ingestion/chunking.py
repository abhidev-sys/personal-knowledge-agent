"""Split normalized text into overlapping, traceable chunks."""
from collections.abc import Sequence

from app.ingestion.text import content_hash, normalize_text
from app.ingestion.types import PlacedSection, Section, TextChunk

SECTION_SEPARATOR = "\n\n"
# Preferred places to cut, best first. "। " is the Hindi sentence end (danda).
_BREAKS = ("\n\n", "\n", ". ", "? ", "! ", "। ", "; ", ", ", " ")


def assemble_document(sections: Sequence[Section]) -> tuple[str, list[PlacedSection]]:
    """Normalize every section, drop empty ones, join them, and remember where each starts.

    The returned text is the source's `content_text`; chunk offsets point into it.
    """
    parts: list[str] = []
    placed: list[PlacedSection] = []
    cursor = 0
    for section in sections:
        text = normalize_text(section.text)
        if not text:
            continue
        if parts:
            cursor += len(SECTION_SEPARATOR)
        placed.append(PlacedSection(text, section.page_number, section.heading, cursor))
        parts.append(text)
        cursor += len(text)
    return SECTION_SEPARATOR.join(parts), placed


def split_spans(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Return (start, end) spans of at most `size` chars with about `overlap` chars shared."""
    if size < 10 or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size/overlap")
    length = len(text)
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < length:
        end = min(pos + size, length)
        if end < length:
            end = _find_cut(text, pos, end, size // 2)
        start, stop = _trim(text, pos, end)
        if stop > start:
            spans.append((start, stop))
        if end >= length:
            break
        nxt = end - overlap
        pos = end if nxt <= pos else _advance_to_word_start(text, nxt, end)
    return spans


def chunk_document(placed: Sequence[PlacedSection], *, size: int, overlap: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    for section in placed:
        for start, end in split_spans(section.text, size, overlap):
            content = section.text[start:end]
            chunks.append(
                TextChunk(
                    sequence=len(chunks),
                    content=content,
                    page_number=section.page_number,
                    section_heading=section.heading,
                    char_start=section.start + start,
                    char_end=section.start + end,
                    content_hash=content_hash(content),
                )
            )
    return chunks


def _find_cut(text: str, pos: int, end: int, min_len: int) -> int:
    window = text[pos:end]
    for separator in _BREAKS:
        index = window.rfind(separator, min_len)
        if index != -1:
            return pos + index + len(separator)
    return end  # no natural boundary: hard cut


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _advance_to_word_start(text: str, nxt: int, limit: int) -> int:
    for index in range(nxt, limit):
        if text[index].isspace():
            return index + 1
    return nxt