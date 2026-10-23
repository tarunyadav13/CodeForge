"""Persisted ChromaDB store for policy chunks, plus the Source Register (Annex B).

One directory (default ``data/chroma``, override with CHROMA_DIR) holds the ChromaDB data and
``source_register.json``. Data written by one process is found by the next, so nothing is
re-ingested on restart. The embedder name is stored with the collection: opening it with a
different embedder is refused, because vectors from different models are not comparable.

The register is a JSON sidecar, NOT a second Chroma collection: with two collections in one
client, ChromaDB 1.5.9 (Windows) intermittently left the chunk collection permanently
unqueryable ("Error creating hnsw segment reader: Nothing found on disk", ~2% of fresh
stores). One collection per client did not show it. The sidecar is written atomically
(temp file, fsync, rename), so a crash leaves either the old register or the new one.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

import chromadb
from chromadb.config import Settings
from pydantic import ValidationError

from api.models import SourceRecord
from rag.embeddings import Embedder

DEFAULT_DIR = "data/chroma"
CHUNKS = "policy_chunks"
REGISTER_FILE = "source_register.json"
_REGISTER_VERSION = 1


class StoreError(Exception):
    """The vector store cannot be used as requested."""


class VectorStore:
    def __init__(self, path: Optional[str | Path] = None, embedder: Optional[Embedder] = None):
        if embedder is None:
            raise StoreError("VectorStore needs an embedder")
        self.path = Path(path or os.environ.get("CHROMA_DIR") or DEFAULT_DIR)
        self.embedder = embedder
        self.path.mkdir(parents=True, exist_ok=True)
        self._register_path = self.path / REGISTER_FILE
        self._register_lock = threading.Lock()  # one process; concurrent ingests are serialised
        self._client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        self._chunks = self._client.get_or_create_collection(
            CHUNKS, metadata={"hnsw:space": "cosine", "embedder": embedder.name}
        )
        stored = (self._chunks.metadata or {}).get("embedder")
        if stored != embedder.name:
            raise StoreError(
                f"store at {self.path} was built with embedder {stored!r}, not {embedder.name!r}; "
                "use the same embedder or a new directory"
            )
        self._read_register()  # fail at open, not at first use, if the register is unreadable

    # ---- chunks -------------------------------------------------------------------------
    def chunk_count(self, doc_id: Optional[str] = None) -> int:
        if doc_id is None:
            return self._chunks.count()
        return len(self._chunks.get(where={"doc_id": doc_id}, include=[])["ids"])

    def replace_document(
        self,
        doc_id: str,
        ids: list[str],
        texts: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """Swap in the new chunks only after they exist, so a failure keeps the old ones."""
        old = self._chunks.get(where={"doc_id": doc_id}, include=[])["ids"]
        if old:
            self._chunks.delete(ids=old)
        self._chunks.add(ids=ids, documents=texts, embeddings=vectors, metadatas=metadatas)

    def query(self, vector: list[float], n: int, doc_ids: Optional[list[str]] = None) -> dict:
        total = self._chunks.count()
        if total == 0 or n < 1:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        where = {"doc_id": {"$in": doc_ids}} if doc_ids else None
        return self._chunks.query(
            query_embeddings=[vector],
            n_results=min(n, total),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    # ---- source register (JSON sidecar) --------------------------------------------------
    def _read_register(self) -> dict[str, dict[str, Any]]:
        """doc_id -> {"record": SourceRecord JSON dict, "content_hash": str, "chunks_indexed": int}."""
        try:
            raw = self._register_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
            if data["version"] != _REGISTER_VERSION:
                raise ValueError(f"unsupported register version {data['version']!r}")
            sources = data["sources"]
            for doc_id, entry in sources.items():
                SourceRecord.model_validate(entry["record"])
                entry["content_hash"], entry["chunks_indexed"]  # noqa: B018 - presence check
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            # Never overwrite or silently reset a register we cannot read: it is the only
            # record of document metadata. A human decides (restore or delete the file).
            raise StoreError(
                f"{self._register_path} is unreadable ({type(exc).__name__}); "
                "restore it or delete it and re-ingest the documents"
            ) from exc
        return sources

    def _write_register(self, sources: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps({"version": _REGISTER_VERSION, "sources": sources}, indent=2, sort_keys=True)
        tmp = self._register_path.with_name(REGISTER_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._register_path)  # atomic: readers see the old or the new file

    def put_source(self, record: SourceRecord, content_hash: str, chunks_indexed: int) -> None:
        with self._register_lock:
            sources = self._read_register()
            sources[record.doc_id] = {
                "record": json.loads(record.model_dump_json()),
                "content_hash": content_hash,
                "chunks_indexed": chunks_indexed,
            }
            self._write_register(sources)

    def get_source_state(self, doc_id: str) -> Optional[Mapping[str, Any]]:
        with self._register_lock:
            entry = self._read_register().get(doc_id)
        if entry is None:
            return None
        return {"content_hash": entry["content_hash"], "chunks_indexed": entry["chunks_indexed"]}

    def list_sources(self) -> list[SourceRecord]:
        with self._register_lock:
            sources = self._read_register()
        records = [SourceRecord.model_validate(e["record"]) for e in sources.values()]
        return sorted(records, key=lambda r: r.doc_id)

    def health(self) -> tuple[str, str]:
        """(status, detail) for the /health vector_store component."""
        try:
            return "ok", f"{self._chunks.count()} chunks, {len(self.list_sources())} sources"
        except Exception as exc:  # noqa: BLE001 - report, don't raise, from a health probe
            return "down", type(exc).__name__
