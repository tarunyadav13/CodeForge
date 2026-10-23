"""Ingest an official document and its Source Register metadata (brief R11, Annex B).

``ingest_document`` is what POST /ingest should call (api/main.py is Member 2's). It
validates the metadata with the frozen ``SourceRecord``, extracts text, chunks it, embeds
the chunks and stores them with the metadata on every chunk. Re-ingesting identical
content is a no-op; ingesting changed content for the same doc_id replaces the old chunks.
Nothing here invents metadata: a missing or invalid field is an ``IngestError``.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import PurePath
from typing import Any, Mapping, Optional, Union

from pydantic import ValidationError

from api.models import IngestResponse, SourceRecord
from rag.chunking import DEFAULT_MAX_WORDS, chunk_document
from rag.safety import looks_like_injection
from rag.store import VectorStore

SUPPORTED = (".txt", ".md", ".pdf")
MAX_BYTES = 20 * 1024 * 1024


class IngestError(ValueError):
    """The document or its metadata cannot be ingested. The message is safe to show."""


def extract_text(data: bytes, filename: str) -> str:
    """Text from a .txt/.md/.pdf upload. PDF pages are joined with form feeds."""
    if len(data) > MAX_BYTES:
        raise IngestError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    ext = PurePath(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise IngestError(f"unsupported file type {ext or '(none)'!r}; supported: {', '.join(SUPPORTED)}")
    if ext == ".pdf":
        return _pdf_text(data)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError("text file is not valid UTF-8") from exc


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise IngestError("PDF is encrypted")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except IngestError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as exc:
        raise IngestError("PDF could not be read (corrupt or unsupported)") from exc
    if not any(p.strip() for p in pages):
        raise IngestError("PDF has no extractable text (scanned? OCR is not implemented)")
    return "\f".join(pages)


def _record(metadata: Union[SourceRecord, Mapping[str, Any]]) -> SourceRecord:
    if isinstance(metadata, SourceRecord):
        record = metadata
    else:
        try:
            record = SourceRecord.model_validate(dict(metadata))
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "?" for e in exc.errors()})
            raise IngestError(f"invalid metadata, check: {', '.join(fields)}") from exc
    if record.effective_to is not None and record.effective_to < record.effective_from:
        raise IngestError("invalid metadata: effective_to is before effective_from")
    if not record.doc_id.strip():
        raise IngestError("invalid metadata: doc_id is empty")
    return record


def _chunk_metadata(record: SourceRecord, section: str, page: Optional[int], index: int, flagged: bool) -> dict:
    # Chroma metadata must be scalars, so absent values are "" / 0 and decoded on retrieval.
    return {
        "doc_id": record.doc_id,
        "title": record.title,
        "issuer": record.issuer,
        "authority_level": record.authority_level,
        "doc_type": record.doc_type,
        "version": record.version,
        "effective_from": record.effective_from.isoformat(),
        "effective_to": record.effective_to.isoformat() if record.effective_to else "",
        "supersedes": record.supersedes,
        "scope_programmes": record.scope_programmes,
        "scope_batches": record.scope_batches,
        "provenance": record.provenance,
        "retrieved_on": record.retrieved_on.isoformat(),
        "synthetic": record.synthetic,
        "section": section,
        "page": page if page is not None else 0,  # 0 = no page information
        "chunk_index": index,
        "injection_suspected": flagged,
    }


def ingest_document(
    store: VectorStore,
    metadata: Union[SourceRecord, Mapping[str, Any]],
    *,
    text: Optional[str] = None,
    data: Optional[bytes] = None,
    filename: Optional[str] = None,
    max_words: int = DEFAULT_MAX_WORDS,
) -> IngestResponse:
    """Index one document. Pass ``text``, or ``data`` + ``filename`` (e.g. an upload)."""
    record = _record(metadata)
    if (text is None) == (data is None):
        raise IngestError("provide exactly one of text or data")
    if data is not None:
        text = extract_text(data, filename or "")
    assert text is not None
    if "\x00" in text:
        raise IngestError("document contains binary data")

    chunks = chunk_document(text, max_words=max_words)
    if not chunks:
        raise IngestError("document has no text to index")

    content_hash = hashlib.sha256((record.model_dump_json() + "\n" + text).encode("utf-8")).hexdigest()
    state = store.get_source_state(record.doc_id)
    if state and state.get("content_hash") == content_hash and store.chunk_count(record.doc_id) == len(chunks):
        return IngestResponse(doc_id=record.doc_id, chunks_indexed=len(chunks), status="unchanged")

    vectors = store.embedder.embed([c.text for c in chunks])
    if len(vectors) != len(chunks):
        raise IngestError("embedding backend returned the wrong number of vectors")
    store.replace_document(
        record.doc_id,
        ids=[f"{record.doc_id}::{c.index}" for c in chunks],
        texts=[c.text for c in chunks],
        vectors=vectors,
        metadatas=[_chunk_metadata(record, c.section, c.page, c.index, looks_like_injection(c.text)) for c in chunks],
    )
    store.put_source(record, content_hash, len(chunks))
    return IngestResponse(
        doc_id=record.doc_id, chunks_indexed=len(chunks), status="replaced" if state else "indexed"
    )
