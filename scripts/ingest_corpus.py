"""Ingest the policy corpus listed in data/source_register.csv through POST /ingest.

    python scripts/ingest_corpus.py [--base-url http://127.0.0.1:8000] [--register data/source_register.csv] [--dry-run]

Goes through the running API (not the vector store directly) so the SQLite Source Register, the ChromaDB chunks and the
audit trail stay consistent. Idempotent: an unchanged document is reported as ``already_ingested``. A row without an
``effective_from`` is SKIPPED, not guessed: the frozen SourceRecord requires the date and precedence depends on it
(see data/README.md). Exit code 1 if any attempted upload failed.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
FILE_IN_PROVENANCE = re.compile(r"\(file (data/documents/[^;)\s]+)")


def document_path(row: dict[str, str]) -> Optional[Path]:
    m = FILE_IN_PROVENANCE.search(row.get("provenance", ""))
    candidate = ROOT / m.group(1) if m else ROOT / "data" / "documents" / "synthetic" / f"{row['doc_id']}.md"
    return candidate if candidate.is_file() else None


def metadata(row: dict[str, str]) -> dict:
    meta = dict(row)
    meta["authority_level"] = int(meta["authority_level"])
    meta["effective_to"] = meta.get("effective_to") or None
    return meta


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--register", default=str(ROOT / "data" / "source_register.csv"))
    ap.add_argument("--dry-run", action="store_true", help="show what would be uploaded, upload nothing")
    args = ap.parse_args(argv)

    with open(args.register, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    failures = 0
    for row in rows:
        doc_id = row["doc_id"]
        if not (row.get("effective_from") or "").strip():
            print(f"SKIP   {doc_id}: effective_from is blank (not guessed; see data/README.md)")
            continue
        path = document_path(row)
        if path is None:
            print(f"FAIL   {doc_id}: document file not found")
            failures += 1
            continue
        if args.dry_run:
            print(f"WOULD  {doc_id}: {path.relative_to(ROOT)}")
            continue
        try:
            r = requests.post(
                f"{args.base_url}/ingest",
                files={"file": (path.name, path.read_bytes()), "metadata": (None, json.dumps(metadata(row)))},
                timeout=600,
            )
        except requests.RequestException as exc:
            print(f"FAIL   {doc_id}: API unreachable ({type(exc).__name__})")
            failures += 1
            continue
        if r.status_code == 200:
            body = r.json()
            print(f"OK     {doc_id}: {body['status']}, {body['chunks_indexed']} chunks")
        else:
            print(f"FAIL   {doc_id}: HTTP {r.status_code} {str(r.json().get('detail', r.text))[:120]}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
