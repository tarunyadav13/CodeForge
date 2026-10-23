"""Embedding backends.

``SentenceTransformerEmbedder`` is the real one (brief Section 5: all-MiniLM-L6-v2 or
BAAI/bge-small-en-v1.5). ``HashEmbedder`` is a deterministic, dependency-free stand-in
for offline tests: bag-of-words feature hashing, so texts that share words are similar.
It is NOT a quality retriever and must never be used to report retrieval results.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Mapping, Optional, Protocol, Sequence

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_STOPWORDS = frozenset(
    "a an the of to in on for and or is are was were be by with at as it this that from "
    "what which who how do does i my me you your can".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9%]+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic offline embedder (stable across processes, unlike ``hash()``)."""

    def __init__(self, dims: int = 256):
        self.dims = dims
        self.name = f"hash-{dims}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for tok in _TOKEN_RE.findall(text.lower()):
            if tok in _STOPWORDS:
                continue
            digest = hashlib.md5(tok.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dims
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0  # empty / stopword-only text: a fixed unit vector, never NaN
            return vec
        return [v / norm for v in vec]


class SentenceTransformerEmbedder:
    """Loads the model lazily on first use (the first call may download it)."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.name = model_name
        self._model = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name)
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return [list(map(float, v)) for v in vectors]


def get_embedder(env: Optional[Mapping[str, str]] = None) -> Embedder:
    """EMBEDDING_BACKEND=hash selects the offline stand-in; EMBEDDING_MODEL names the model."""
    env = os.environ if env is None else env
    if (env.get("EMBEDDING_BACKEND") or "").strip().lower() == "hash":
        return HashEmbedder()
    return SentenceTransformerEmbedder((env.get("EMBEDDING_MODEL") or "").strip() or DEFAULT_MODEL)
