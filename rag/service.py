"""Process-wide RAG service and the thin adapters the API's hooks call (api/main.py).

api/main.py looks up three hooks by name; they live here so neither side's real interface changes:

* ``ingest_file(file_path, metadata) -> {"chunks_indexed": int, "status": str}``  (POST /ingest)
* ``health() -> {"status": "ok"|"degraded"|"down", "detail": str}``              (GET /health, vector_store)
* ``has_document(doc_id) -> bool``  true only if the vector store really holds chunks for the document, so a
  document is never reported "already ingested" when only a register row exists.

``IngestError`` (a ValueError) is deliberately not caught here: the API turns it into a client error (HTTP 422).
The store is created lazily on first use (CHROMA_DIR, EMBEDDING_* are read then); tests inject their own with
``set_store``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from rag.embeddings import get_embedder
from rag.ingest import ingest_document
from rag.retriever import Retriever
from rag.store import StoreError, VectorStore

_lock = threading.Lock()
_store: Optional[VectorStore] = None


def get_store() -> VectorStore:
    global _store
    with _lock:
        if _store is None:
            _store = VectorStore(embedder=get_embedder())
        return _store


def set_store(store: Optional[VectorStore]) -> None:
    """Install (or clear, with None) the shared store; used by tests."""
    global _store
    with _lock:
        _store = store


def get_retriever(**kwargs: Any) -> Retriever:
    return Retriever(get_store(), **kwargs)


def ingest_file(file_path: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(file_path)
    result = ingest_document(get_store(), metadata, data=path.read_bytes(), filename=path.name)
    return {"chunks_indexed": result.chunks_indexed, "status": result.status}


def has_document(doc_id: str) -> bool:
    try:
        return get_store().chunk_count(doc_id) > 0
    except Exception:  # noqa: BLE001 - an unreadable store means "not indexed", so the upload is retried
        return False


def health() -> dict[str, str]:
    try:
        status, detail = get_store().health()
    except StoreError as exc:
        return {"status": "down", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "detail": f"vector store unavailable ({type(exc).__name__})"}
    return {"status": status, "detail": detail}
