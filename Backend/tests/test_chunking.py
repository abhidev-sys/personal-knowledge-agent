import pytest

from app.ingestion.chunking import assemble_document, chunk_document, split_spans
from app.ingestion.text import content_hash
from app.ingestion.types import Section

ENGLISH = "The quick brown fox jumps over the lazy dog. " * 200
HINDI = "यह एक परीक्षण वाक्य है। " * 100


def test_assemble_places_sections_at_correct_offsets():
    text, placed = assemble_document([Section("  Hello  "), Section(""), Section("World")])
    assert text == "Hello\n\nWorld"
    assert [p.start for p in placed] == [0, 7]
    assert text[placed[1].start : placed[1].start + 5] == "World"


def test_short_text_is_one_chunk():
    assert split_spans("hello world", 100, 10) == [(0, 11)]


def test_empty_text_has_no_chunks():
    assert split_spans("", 100, 10) == []


@pytest.mark.parametrize("size,overlap", [(5, 1), (100, 100), (100, -1)])
def test_invalid_parameters_raise(size, overlap):
    with pytest.raises(ValueError):
        split_spans("some text", size, overlap)


@pytest.mark.parametrize("text", [ENGLISH, HINDI, "x" * 1000])
def test_chunk_invariants(text):
    size, overlap = 300, 50
    spans = split_spans(text, size, overlap)
    assert len(spans) > 1
    starts = [s for s, _ in spans]
    assert starts == sorted(set(starts))  # strictly increasing
    for start, end in spans:
        assert 0 < end - start <= size
        assert text[start:end] == text[start:end].strip()
    # every non-whitespace character is inside at least one chunk
    for index, char in enumerate(text):
        if not char.isspace():
            assert any(s <= index < e for s, e in spans)


def test_consecutive_chunks_overlap_on_normal_prose():
    spans = split_spans(ENGLISH, 300, 80)
    for (_, previous_end), (next_start, _) in zip(spans, spans[1:]):
        assert next_start < previous_end


def test_prefers_sentence_and_paragraph_boundaries():
    text = ("Alpha sentence one. " * 10).strip() + "\n\n" + ("Beta sentence two. " * 10).strip()
    first_start, first_end = split_spans(text, 250, 20)[0]
    assert text[first_start:first_end].endswith(("one.", "two."))


def test_chunks_keep_page_heading_offsets_and_hash():
    sections = [
        Section("Alpha " * 100, page_number=1, heading="A"),
        Section("Beta " * 100, page_number=2, heading="B"),
    ]
    text, placed = assemble_document(sections)
    chunks = chunk_document(placed, size=200, overlap=30)
    assert [c.sequence for c in chunks] == list(range(len(chunks)))
    assert {c.page_number for c in chunks} == {1, 2}
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.content
        assert chunk.content_hash == content_hash(chunk.content)
        assert chunk.section_heading == ("A" if chunk.page_number == 1 else "B")