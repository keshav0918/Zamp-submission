"""SQLite ledger with persistent local and in-memory Vercel connections."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


HISTORY = Path(__file__).resolve().parent / "data" / "web-runs.sqlite3"


@contextmanager
def open_ledger(*, db_path=None, write=False):
    with closing(sqlite3.connect(HISTORY if db_path is None else db_path, timeout=10)) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS ap_runs (
                id INTEGER PRIMARY KEY, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                scope TEXT NOT NULL, filename TEXT NOT NULL,
                vendor TEXT NOT NULL, invoice TEXT NOT NULL, po TEXT,
                status TEXT NOT NULL, outcome TEXT NOT NULL, quantities TEXT NOT NULL,
                variance TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ap_runs_approved
            ON ap_runs (scope, vendor, invoice) WHERE status = 'Approved';
            CREATE INDEX IF NOT EXISTS ap_runs_history ON ap_runs (scope, id DESC);
        """)
        with connection:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
