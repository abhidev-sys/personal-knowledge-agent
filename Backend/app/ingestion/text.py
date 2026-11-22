"""Text normalization, decoding and hashing shared by every ingestion path."""
import codecs
import hashlib
import re
import unicodedata

from app.core.exceptions import IngestionError

# Zero-width space, word joiner, BOM. NOT U+200C/U+200D: Indic scripts need those joiners.
_REMOVED_CHARS = {ord(c): None for c in "\u200b\u2060\ufeff"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(raw: str) -> str:
    """Canonical form used for storage, hashing and chunking. Idempotent."""
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_REMOVED_CHARS)
    text = _CONTROL.sub("", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def content_hash(text: str, *, title: str | None = None) -> str:
    """SHA-256 hex digest (64 chars). A title, when given, is part of the identity."""
    digest = hashlib.sha256()
    if title is not None:
        digest.update(title.encode("utf-8", errors="replace"))
        digest.update(b"\x00")
    digest.update(text.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def clean_title(value: str | None, fallback: str, max_length: int = 500) -> str:
    candidate = _WHITESPACE.sub(" ", normalize_text(value or "")).strip()
    return (candidate or fallback)[:max_length]


def decode_text_bytes(data: bytes) -> str:
    """Decode an uploaded text file. UTF-8 (with or without BOM) or UTF-16 with BOM only."""
    encoding = "utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise IngestionError("The file is not valid UTF-8 (or UTF-16) text.") from exc