import pytest

from app.db.types import EMBEDDING_DIM
from app.providers.embeddings import HashingEmbedder, embed_in_batches


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # vectors are already L2-normalized


def test_vectors_have_the_database_dimension_and_unit_length():
    (vector,) = HashingEmbedder().embed(["retrieval augmented generation"])
    assert len(vector) == EMBEDDING_DIM
    assert sum(v * v for v in vector) == pytest.approx(1.0)


def test_deterministic():
    e = HashingEmbedder()
    assert e.embed(["same text"]) == e.embed(["same text"])


def test_related_text_scores_higher_than_unrelated():
    query, near, far = HashingEmbedder().embed(
        [
            "retrieval augmented generation",
            "retrieval augmented generation with vector search",
            "cooking pasta with tomato sauce",
        ]
    )
    assert cosine(query, near) > cosine(query, far)


def test_empty_text_gives_a_usable_vector():
    (vector,) = HashingEmbedder().embed(["   "])
    assert sum(v * v for v in vector) == pytest.approx(1.0)


def test_batching_splits_calls():
    class Counting(HashingEmbedder):
        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(len(texts))
            return super().embed(texts)

    provider = Counting()
    vectors = embed_in_batches(provider, ["a"] * 5, 2)
    assert provider.calls == [2, 2, 1] and len(vectors) == 5


def test_wrong_dimension_provider_is_refused():
    class Small:
        model_id = "small"
        dimensions = 3

        def embed(self, texts):
            return [[0.0, 0.0, 1.0] for _ in texts]

    with pytest.raises(ValueError, match="dimensions"):
        embed_in_batches(Small(), ["x"], 8)


def test_provider_returning_wrong_count_is_refused():
    class Broken(HashingEmbedder):
        def embed(self, texts):
            return super().embed(texts[:-1])

    with pytest.raises(ValueError, match="wrong number"):
        embed_in_batches(Broken(), ["a", "b"], 8)