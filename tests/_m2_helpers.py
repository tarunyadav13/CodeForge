"""Shared fixtures/helpers for Member 2's backend tests. Everything runs on temporary SQLite
files; no test touches data/student_services.db or needs a network or an API key."""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import data_access as da  # noqa: E402
from scripts import gen_data  # noqa: E402
from scripts.load_students import load_directory  # noqa: E402

AS_OF = "2026-10-06"


@pytest.fixture(scope="session")
def gen_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic")
    dataset = gen_data.build_dataset(gen_data.DEFAULT_SEED)
    report = gen_data.write_dataset(dataset, out, seed=gen_data.DEFAULT_SEED, llm_report=None)
    return out, report


@pytest.fixture(scope="session")
def loaded_db(gen_dir, tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "student_services.db"
    report = load_directory(gen_dir[0], path)
    assert report.ok, "fixture data must load cleanly"
    return path


@pytest.fixture()
def db_copy(loaded_db, tmp_path):
    """A private, writable copy so a test may corrupt/alter it."""
    target = tmp_path / "copy.db"
    shutil.copy(loaded_db, target)
    return target


def raw_conn(path, *, enforce: bool = True) -> sqlite3.Connection:
    """Plain connection for deliberately breaking data in tests."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA foreign_keys = {'ON' if enforce else 'OFF'}")
    return conn


def corrupt(path, sql: str, params: tuple = ()) -> None:
    """Run SQL with constraints disabled so invalid data can be planted."""
    conn = raw_conn(path, enforce=False)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(sql, params)
    conn.commit()
    conn.close()
