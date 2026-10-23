"""Load CSV data (Annex C schema) into SQLite. Judges use this loader.

    python scripts/load_students.py --dir test_students/
    python scripts/load_students.py --dir data/synthetic --db C:\\tmp\\demo.db

Looks for ``students.csv``, ``courses.csv``, ``attendance.csv``, ``results.csv`` and,
optionally, ``rule_registry.csv`` and ``source_register.csv`` (case-insensitive) in the
directory. Any subset is fine; missing files are skipped.

Behaviour
* Creates the database / tables if they do not exist (idempotent).
* UPSERT semantics: re-running the same load changes nothing; changed rows are updated.
* Each row is loaded in its own savepoint. A bad row is rejected and reported with its
  line number; good rows still load (use --atomic to roll everything back instead).
* The first occurrence of a duplicate key within one file wins; later ones are rejected.
* Reserved judge IDs (S9000-S9999, JDG*) are ACCEPTED here: judges load their own data.

Exit codes: 0 = everything loaded, 1 = some rows/files rejected, 2 = usage or I/O error.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import data_access as da  # noqa: E402
from scripts.csv_schema import (COLUMNS, KEY_COLUMNS, TABLE_ORDER, convert_row,  # noqa: E402
                                normalise_header)

_UPSERTS = {
    "source_register": da.register_source,
    "courses": da.upsert_course,
    "students": da.upsert_student,
    "rule_registry": da.upsert_rule,
    "attendance": da.upsert_attendance,
    "results": da.upsert_result,
}


@dataclass
class TableReport:
    table: str
    file: Optional[str] = None
    rows_read: int = 0
    rows_loaded: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (csv line number, reason)
    file_error: Optional[str] = None


@dataclass
class LoadReport:
    directory: str
    db_path: str
    tables: list[TableReport] = field(default_factory=list)
    rolled_back: bool = False

    @property
    def rows_loaded(self) -> int:
        return sum(t.rows_loaded for t in self.tables)

    @property
    def rows_rejected(self) -> int:
        return sum(len(t.rejected) + (1 if t.file_error else 0) for t in self.tables)

    @property
    def files_found(self) -> int:
        return sum(1 for t in self.tables if t.file)

    @property
    def ok(self) -> bool:
        return self.files_found > 0 and self.rows_rejected == 0 and not self.rolled_back


def _find_files(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix.lower() == ".csv" and p.stem.lower() in COLUMNS:
            found.setdefault(p.stem.lower(), p)
    return found


def _clean_error(exc: Exception) -> str:
    msg = str(exc)
    return msg if len(msg) <= 160 else msg[:157] + "..."


def _load_table(conn: sqlite3.Connection, table: str, path: Path) -> TableReport:
    rep = TableReport(table=table, file=path.name)
    upsert = _UPSERTS[table]
    seen: set[tuple] = set()
    try:
        fh = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        rep.file_error = f"cannot read file: {exc.strerror or exc}"
        return rep
    with fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            rep.file_error = "file is empty (no header row)"
            return rep
        headers = [normalise_header(h) for h in reader.fieldnames]
        missing = [c for c in COLUMNS[table] if c not in headers
                   and c not in ("effective_to", "supersedes", "scope_programmes", "scope_batches", "synthetic")]
        if missing:
            rep.file_error = f"missing required column(s): {', '.join(missing)}"
            return rep
        reader.fieldnames = headers
        for raw in reader:
            line = reader.line_num
            rep.rows_read += 1
            try:
                row = convert_row(table, raw)
                key = tuple(str(row[c]).casefold() if c in ("course_code", "exam_session") else row[c]
                            for c in KEY_COLUMNS[table])
                if key in seen:
                    raise ValueError("duplicate key in this file (first occurrence kept)")
                conn.execute("SAVEPOINT row_load")
                try:
                    upsert(conn, row)
                except (sqlite3.IntegrityError, da.DataAccessError) as exc:
                    conn.execute("ROLLBACK TO row_load")
                    raise ValueError(_clean_error(exc)) from None
                finally:
                    conn.execute("RELEASE row_load")
                seen.add(key)
                rep.rows_loaded += 1
            except ValueError as exc:
                rep.rejected.append((line, str(exc)))
    return rep


def load_directory(directory: Union[str, Path], db_path: Union[str, Path, None] = None,
                   *, atomic: bool = False) -> LoadReport:
    """Load every recognised CSV in ``directory``. Returns a LoadReport (never raises on bad rows)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")
    path = da.init_db(db_path)
    report = LoadReport(directory=str(directory), db_path=str(path))
    files = _find_files(directory)
    conn = da.connect(path)
    try:
        conn.isolation_level = None  # explicit transaction control
        conn.execute("BEGIN")
        for table in TABLE_ORDER:
            if table in files:
                report.tables.append(_load_table(conn, table, files[table]))
            else:
                report.tables.append(TableReport(table=table))
        if atomic and report.rows_rejected:
            conn.execute("ROLLBACK")
            report.rolled_back = True
        else:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return report


def format_report(report: LoadReport) -> str:
    lines = [f"Loaded from {report.directory} into {report.db_path}"]
    for t in report.tables:
        if not t.file:
            lines.append(f"  {t.table:<16} (no file)")
            continue
        status = f"read {t.rows_read}, loaded {t.rows_loaded}, rejected {len(t.rejected)}"
        if t.file_error:
            status = f"FILE REJECTED: {t.file_error}"
        lines.append(f"  {t.table:<16} {t.file}: {status}")
        for line_no, reason in t.rejected[:20]:
            lines.append(f"      line {line_no}: {reason}")
        if len(t.rejected) > 20:
            lines.append(f"      ... {len(t.rejected) - 20} more rejected rows")
    if report.rolled_back:
        lines.append("ATOMIC MODE: rejections found, nothing was written.")
    lines.append(f"Total: {report.rows_loaded} rows loaded, {report.rows_rejected} rejected.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Load Annex C CSV files into the SQLite database.")
    parser.add_argument("--dir", required=True, help="directory containing students.csv, courses.csv, ...")
    parser.add_argument("--db", help="database path (default: $STUDENT_DB_PATH or data/student_services.db)")
    parser.add_argument("--atomic", action="store_true", help="write nothing if any row is rejected")
    args = parser.parse_args(argv)
    try:
        report = load_directory(args.dir, args.db, atomic=args.atomic)
    except (OSError, da.DataAccessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    if report.files_found == 0:
        print(f"ERROR: no recognised CSV files in {args.dir} "
              f"(expected: {', '.join(t + '.csv' for t in TABLE_ORDER)})", file=sys.stderr)
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
