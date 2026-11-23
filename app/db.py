"""SQLite connection, schema, snapshot and read-guard helpers.

Two kinds of connections are used:

* writer connections (one per ingest request) open with ``BEGIN IMMEDIATE``;
* reader connections are opened ``mode=ro``, wrapped in a deferred read
  transaction so every read in a request sees one committed snapshot, and
  carry an authorizer that only permits reads from the small aggregate tables.
  Raw core/measurement tables are deliberately *not* readable from these
  connections.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

# Tables the reference endpoint is allowed to read. Everything else (notably
# `measurement` and `core`) is denied by the SQLite authorizer.
ALLOWED_READ_TABLES: frozenset[str] = frozenset(
    {"year_stat", "tree_year", "year_value_cum", "meta", "sqlite_master", "sqlite_temp_master"}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0');

CREATE TABLE IF NOT EXISTS core (
    tree_no      INTEGER NOT NULL CHECK (tree_no >= 1),
    core_no      INTEGER NOT NULL CHECK (core_no >= 1),
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    updated_at   TEXT NOT NULL,
    data_version INTEGER NOT NULL,
    PRIMARY KEY (tree_no, core_no)
) WITHOUT ROWID;

-- Raw measurements. The reference query is never allowed to read this table.
CREATE TABLE IF NOT EXISTS measurement (
    tree_no     INTEGER NOT NULL,
    core_no     INTEGER NOT NULL,
    year        INTEGER NOT NULL CHECK (year BETWEEN 1 AND 9999),
    index_value INTEGER NOT NULL CHECK (index_value >= 0),
    PRIMARY KEY (tree_no, core_no, year),
    FOREIGN KEY (tree_no, core_no) REFERENCES core(tree_no, core_no) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS measurement_tree_year
    ON measurement(tree_no, year);

-- Per tree, per year median of that tree's core indices, stored doubled so a
-- median over an even number of cores stays exact (x.5 -> integer x2).
CREATE TABLE IF NOT EXISTS tree_year (
    year        INTEGER NOT NULL CHECK (year BETWEEN 1 AND 9999),
    tree_no     INTEGER NOT NULL CHECK (tree_no >= 1),
    median_x2   INTEGER NOT NULL CHECK (median_x2 >= 0),
    core_count  INTEGER NOT NULL CHECK (core_count >= 1),
    PRIMARY KEY (year, tree_no)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS tree_year_by_tree
    ON tree_year(tree_no, year);
CREATE INDEX IF NOT EXISTS tree_year_value
    ON tree_year(year, median_x2);

-- One row per year. tree_count is the number of sampled trees; slot_count is
-- the number of distinct median values in year_value_cum.
CREATE TABLE IF NOT EXISTS year_stat (
    year       INTEGER PRIMARY KEY CHECK (year BETWEEN 1 AND 9999),
    tree_count INTEGER NOT NULL CHECK (tree_count >= 1),
    slot_count INTEGER NOT NULL CHECK (slot_count >= 1)
) WITHOUT ROWID;

-- Cumulative histogram of per-tree medians for a year. One row per distinct
-- value (slot). `cum` is the number of tree medians <= value_x2, which makes
-- order-statistic selection possible with O(log slot_count) point lookups.
CREATE TABLE IF NOT EXISTS year_value_cum (
    year     INTEGER NOT NULL,
    slot     INTEGER NOT NULL CHECK (slot >= 1),
    value_x2 INTEGER NOT NULL CHECK (value_x2 >= 0),
    cnt      INTEGER NOT NULL CHECK (cnt >= 1),
    cum      INTEGER NOT NULL CHECK (cum >= 1),
    PRIMARY KEY (year, slot),
    UNIQUE (year, value_x2)
) WITHOUT ROWID;
"""


def _db_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def connect_write(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a connection that may write. Caller owns transactions."""
    conn = sqlite3.connect(str(_db_path(path)), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_READ and arg1 is not None and arg1 not in ALLOWED_READ_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@dataclass
class ReadMetrics:
    """Cheap accounting of rows returned to the reference query.

    Every read in the reference path goes through :class:`ReadSession`, so this
    is the number of SQLite rows the query actually consumed. EXPLAIN tests
    separately assert that all of those lookups are B-tree SEARCHes (no SCAN).
    """

    rows_read: int = 0
    statements: int = 0
    slot_probes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows_read": self.rows_read,
            "statements": self.statements,
            "slot_probes": self.slot_probes,
        }


@dataclass
class ReadSession:
    conn: sqlite3.Connection
    metrics: ReadMetrics = field(default_factory=ReadMetrics)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        self.metrics.statements += 1
        row = self.conn.execute(sql, params).fetchone()
        if row is not None:
            self.metrics.rows_read += 1
        return row

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        self.metrics.statements += 1
        rows = self.conn.execute(sql, params).fetchall()
        self.metrics.rows_read += len(rows)
        return rows


def connect_read(path: str | os.PathLike[str], *, restricted: bool = True) -> sqlite3.Connection:
    """Open a read-only connection.

    ``restricted=True`` installs the authorizer used by the reference endpoint.
    ``restricted=False`` is used only by administrative read endpoints (e.g. a
    core's raw series) and by health checks.
    """
    uri = "file:" + quote(str(_db_path(path))) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA query_only=ON")
    if restricted:
        conn.set_authorizer(_authorizer)
    return conn


@contextmanager
def read_snapshot(path: str | os.PathLike[str], *, restricted: bool = True) -> Iterator[ReadSession]:
    """Yield one consistent read snapshot; always rolls back on exit."""
    conn = connect_read(path, restricted=restricted)
    try:
        conn.execute("BEGIN")
        yield ReadSession(conn)
    finally:
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()


def init_db(path: str | os.PathLike[str]) -> None:
    """Create the schema if needed and ensure WAL mode."""
    p = _db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def read_data_version(path: str | os.PathLike[str]) -> int:
    with read_snapshot(path, restricted=False) as session:
        row = session.fetchone("SELECT value FROM meta WHERE key='data_version'")
        return int(row["value"]) if row is not None else 0


def busy_retry(fn, *, attempts: int = 6):
    """Run a write operation, retrying only on SQLITE_BUSY/locked."""
    delay = 0.05
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing dependent
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            time.sleep(delay * (2**attempt))
    assert last is not None
    raise last
