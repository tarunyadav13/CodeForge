"""Ingestion and retrieval for Member 1. Entry points: ``ingest_document`` and ``Retriever``."""
from rag.embeddings import HashEmbedder, SentenceTransformerEmbedder, get_embedder
from rag.ingest import IngestError, extract_text, ingest_document
from rag.precedence import PrecedenceResult, resolve_precedence
from rag.retriever import RetrievedChunk, Retriever
from rag.safety import EVIDENCE_RULE, format_evidence, looks_like_injection
from rag.store import StoreError, VectorStore

__all__ = [
    "EVIDENCE_RULE", "HashEmbedder", "IngestError", "PrecedenceResult", "RetrievedChunk",
    "Retriever", "SentenceTransformerEmbedder", "StoreError", "VectorStore", "extract_text",
    "format_evidence", "get_embedder", "ingest_document", "looks_like_injection",
    "resolve_precedence",
]
