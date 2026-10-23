"""Prepare a freshly started host (empty disk) for the API. Idempotent; run before uvicorn.

    python scripts/bootstrap_deploy.py [--strict]

1. Creates the SQLite schema and, if the students table is empty, loads Member 2's SYNTHETIC students from data/synthetic.
2. Ingests every row of data/source_register.csv that has an effective_from into ChromaDB AND the SQLite Source Register
   (the same two writes POST /ingest makes). Rows without a date (the three real PDFs) are skipped, never guessed.
   Documents already indexed (same content hash and chunks present) are left alone, so a restart on a persistent disk
   does no work.

Without --strict a failed corpus ingest (for example the embedding model cannot be downloaded) is logged and the API
still starts: personal-record tools keep working and /health reports the empty vector store. With --strict any failure
exits 1. Student data is synthetic only; this script never touches real personal data.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.models import SourceRecord  # noqa: E402
from db import data_access as da  # noqa: E402
from scripts.ingest_corpus import document_path, metadata  # noqa: E402
from scripts.load_students import load_directory  # noqa: E402


def ensure_students(db_path=None) -> str:
    da.init_db(db_path)
    with da.db_session(db_path, read_only=True) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    if n:
        return f"students: {n} already loaded"
    report = load_directory(ROOT / "data" / "synthetic", db_path)
    if not report.ok:
        raise RuntimeError("synthetic student data failed to load")
    return f"students: loaded {report.rows_loaded} synthetic rows"


def ensure_corpus(db_path=None) -> list[str]:
    import csv

    from rag import service  # imported late: loads chromadb

    lines = []
    with open(ROOT / "data" / "source_register.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        doc_id = row["doc_id"]
        if not (row.get("effective_from") or "").strip():
            lines.append(f"skip {doc_id}: effective_from unknown (not guessed)")
            continue
        path = document_path(row)
        if path is None:
            raise FileNotFoundError(f"{doc_id}: document file not found")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with da.db_session(db_path, read_only=True) as conn:
            existing = da.get_source(conn, doc_id)
        if existing and existing.get("content_sha256") == sha and service.has_document(doc_id):
            lines.append(f"ok   {doc_id}: already indexed")
            continue
        result = service.ingest_file(str(path), metadata(row))
        with da.db_session(db_path) as conn:
            da.register_source(conn, SourceRecord.model_validate(metadata(row)), content_sha256=sha,
                               chunks_indexed=result["chunks_indexed"])
        lines.append(f"ok   {doc_id}: {result['status']}, {result['chunks_indexed']} chunks")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="exit 1 on any failure, including the corpus ingest")
    args = ap.parse_args(argv)
    try:
        print("bootstrap:", ensure_students())
    except Exception as exc:  # noqa: BLE001 - without the database the API is useless: always fatal
        print(f"bootstrap: FATAL student database setup failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    try:
        for line in ensure_corpus():
            print("bootstrap:", line)
    except Exception as exc:  # noqa: BLE001
        print(f"bootstrap: WARNING corpus ingest failed ({type(exc).__name__}: {exc}); the API will report an empty vector store",
              file=sys.stderr)
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
