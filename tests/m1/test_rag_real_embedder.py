"""Opt-in check of the real sentence-transformers model (downloads it on first use).

    $env:RUN_LIVE_EMBEDDINGS = "1"
    python -m pytest tests/m1/test_rag_real_embedder.py -q -s

Skipped by default so the offline suite never touches the network. Fixture text is
synthetic test content, not university policy.
"""
import os
from datetime import date

import pytest

from rag import Retriever, SentenceTransformerEmbedder, VectorStore, ingest_document

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_EMBEDDINGS") != "1", reason="set RUN_LIVE_EMBEDDINGS=1"),
]

DOCS = {
    "TEST-ATT": "1. Attendance\n1.1 A student needs at least 75% attendance in a course to be allowed to sit the end-semester examination.",
    "TEST-HOSTEL": "1. Hostel\n1.1 Hostel rent must be paid before the 10th of every month; a late fee applies afterwards.",
    "TEST-SUPPL": "1. Supplementary examination\n1.1 A student who failed a course may apply for the supplementary examination within ten days of the result.",
}


def test_real_embeddings_rank_the_right_document_first(tmp_path):
    store = VectorStore(tmp_path / "chroma", SentenceTransformerEmbedder())
    for doc_id, text in DOCS.items():
        ingest_document(
            store,
            dict(doc_id=doc_id, title=doc_id, issuer="test", authority_level=1, doc_type="regulation",
                 version="1", effective_from=date(2024, 1, 1), provenance="unit-test fixture",
                 retrieved_on=date(2026, 10, 23), synthetic="Y"),
            text=text,
        )
    r = Retriever(store, min_score=0.0)
    for question, expected in [
        ("How much attendance do I need to write the final exam?", "TEST-ATT"),
        ("When is the hostel rent due?", "TEST-HOSTEL"),
        ("How do I apply to re-take a failed paper?", "TEST-SUPPL"),
    ]:
        hits = r.retrieve(question, top_k=3)
        print(f"{question!r} -> {[(h.doc_id, round(h.score, 3)) for h in hits]}")
        assert hits[0].doc_id == expected
    off_topic = r.retrieve("scholarship for studying in Antarctica", top_k=3)
    print("off-topic ->", [(h.doc_id, round(h.score, 3)) for h in off_topic])
