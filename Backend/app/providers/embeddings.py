"""Embedding provider interface + a deterministic test provider.

Real providers (Ollama, OpenAI...) arrive in Phase 16 and implement the same Protocol.
"""
import hashlib
import math
import string
from collections.abc import Sequence
from typing import Protocol

from app.db.types import EMBEDDING_DIM


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Test-only: hashes each word into one of 768 buckets, then L2-normalizes.

    Deterministic and free, so retrieval tests need no paid API. It measures word overlap,
    NOT meaning: it is not a substitute for a real embedding model.
    """

    @property
    def model_id(self) -> str:
        return "test-hashing-768"

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for raw in text.lower().split():
            token = raw.strip(string.punctuation + "।")
            if not token:
                continue
            bucket = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
            vector[bucket % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0  # a zero vector would break cosine distance
            return vector
        return [v / norm for v in vector]


def embed_in_batches(
    provider: EmbeddingProvider, texts: Sequence[str], batch_size: int
) -> list[list[float]]:
    """Embed texts in batches, refusing vectors that do not match the database column."""
    if provider.dimensions != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding model produces {provider.dimensions} dimensions; "
            f"the database expects {EMBEDDING_DIM}."
        )
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = provider.embed(batch)
        if len(result) != len(batch):
            raise ValueError("Embedding provider returned the wrong number of vectors.")
        if any(len(vector) != EMBEDDING_DIM for vector in result):
            raise ValueError("Embedding provider returned a vector of the wrong size.")
        vectors.extend(result)
    return vectors