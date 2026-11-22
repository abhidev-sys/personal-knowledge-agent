import pytest

from app.core.exceptions import IngestionError
from app.ingestion.text import clean_title, content_hash, decode_text_bytes, normalize_text


def test_normalizes_line_endings():
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_removes_nul_and_zero_width_space():
    assert normalize_text("x\x00y\u200bz") == "xyz"


def test_keeps_zero_width_joiner_needed_by_indic_scripts():
    text = "क्\u200dष"
    assert normalize_text(text) == text


def test_applies_nfc():
    assert normalize_text("e\u0301") == "\u00e9"


def test_collapses_spaces_and_blank_lines():
    assert normalize_text("a   \t b  \n\n\n\n\nc   ") == "a b\n\nc"


def test_non_breaking_space_becomes_space():
    assert normalize_text("a\u00a0b") == "a b"


@pytest.mark.parametrize("sample", ["  Hello \r\n\r\n\r\n World  ", "नमस्ते   दुनिया\n\n\n\nok", "x\x00 y"])
def test_normalize_is_idempotent(sample):
    once = normalize_text(sample)
    assert normalize_text(once) == once


def test_hash_is_stable_64_hex_and_content_sensitive():
    first = content_hash("hello")
    assert first == content_hash("hello")
    assert len(first) == 64
    assert first != content_hash("hello!")


def test_title_is_part_of_the_hash_when_given():
    assert content_hash("body", title="A") != content_hash("body", title="B")
    assert content_hash("body", title="A") != content_hash("body")


def test_decode_utf8_with_and_without_bom():
    assert decode_text_bytes("hello".encode("utf-8")) == "hello"
    assert decode_text_bytes(b"\xef\xbb\xbfhello") == "hello"


def test_decode_utf16_with_bom():
    assert decode_text_bytes("नमस्ते".encode("utf-16")) == "नमस्ते"


def test_decode_rejects_binary_garbage():
    with pytest.raises(IngestionError):
        decode_text_bytes(b"\x80\x81\x82\xff")


def test_clean_title_collapses_and_falls_back_and_truncates():
    assert clean_title("  Hello \n World  ", "x") == "Hello World"
    assert clean_title(None, "Untitled") == "Untitled"
    assert clean_title("   ", "Untitled") == "Untitled"
    assert len(clean_title("a" * 900, "x")) == 500