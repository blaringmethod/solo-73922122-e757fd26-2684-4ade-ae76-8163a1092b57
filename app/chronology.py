"""Chronology ingest and reference-value queries.

Aggregate layout
----------------
``tree_year``   one row per (year, tree): median of that tree's core indices,
                stored doubled so an even number of cores stays exact.
``year_stat``   one row per year: number of sampled trees and histogram slots.
``year_value_cum``
                cumulative histogram over the tree medians of a year. Slot
                ``s`` holds a distinct doubled median and ``cum`` = number of
                tree medians <= that value.

The reference query never touches ``measurement``/``core``. For a year it
reads a handful of rows and performs two order-statistic searches over the
cumulative histogram, so the number of rows read is O(log number of distinct
tree medians), i.e. sublinear in the year's tree count. Excluding a tree only
subtracts that one tree's median from the counts on the fly; no per-tree
"excluded" answer is ever stored.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .db import (
    ReadSession,
    busy_retry,
    connect_write,
    read_snapshot,
)
from .models import ReportRequest

MIN_TREES = 3


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row is not None else 0


def _set_meta_int(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _median_x2(values: list[int]) -> int:
    """Median of integers in doubled units (exact for even counts too)."""
    n = len(values)
    if n == 0:
        raise ValueError("median of empty sequence")
    if n % 2:
        return 2 * values[n // 2]
    return values[n // 2 - 1] + values[n // 2]


def _rebuild_tree_year(conn: sqlite3.Connection, tree_no: int, year: int) -> None:
    rows = conn.execute(
        "SELECT index_value FROM measurement "
        "WHERE tree_no=? AND year=? ORDER BY index_value",
        (tree_no, year),
    ).fetchall()
    if not rows:
        conn.execute(
            "DELETE FROM tree_year WHERE year=? AND tree_no=?", (year, tree_no)
        )
        return
    values = [int(r["index_value"]) for r in rows]
    conn.execute(
        "INSERT INTO tree_year(year, tree_no, median_x2, core_count) VALUES(?,?,?,?) "
        "ON CONFLICT(year, tree_no) DO UPDATE SET "
        "median_x2=excluded.median_x2, core_count=excluded.core_count",
        (year, tree_no, _median_x2(values), len(values)),
    )


def _rebuild_year(conn: sqlite3.Connection, year: int) -> None:
    rows = conn.execute(
        "SELECT median_x2, COUNT(*) AS cnt FROM tree_year "
        "WHERE year=? GROUP BY median_x2 ORDER BY median_x2",
        (year,),
    ).fetchall()
    conn.execute("DELETE FROM year_value_cum WHERE year=?", (year,))
    if not rows:
        conn.execute("DELETE FROM year_stat WHERE year=?", (year,))
        return

    cumulative = 0
    slots: list[tuple[int, int, int, int, int]] = []
    for slot, row in enumerate(rows, start=1):
        cnt = int(row["cnt"])
        cumulative += cnt
        slots.append((year, slot, int(row["median_x2"]), cnt, cumulative))
    conn.executemany(
        "INSERT INTO year_value_cum(year, slot, value_x2, cnt, cum) "
        "VALUES(?,?,?,?,?)",
        slots,
    )
    conn.execute(
        "INSERT INTO year_stat(year, tree_count, slot_count) VALUES(?,?,?) "
        "ON CONFLICT(year) DO UPDATE SET "
        "tree_count=excluded.tree_count, slot_count=excluded.slot_count",
        (year, cumulative, len(slots)),
    )


def _apply_report_tx(conn: sqlite3.Connection, request: ReportRequest) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_version = _meta_int(conn, "data_version") + 1

    touched_trees: set[int] = set()
    tree_years: dict[int, set[int]] = {}
    affected_years: set[int] = set()
    measurements_written = 0

    for core in request.cores:
        touched_trees.add(core.tree_no)
        old_years = {
            int(r["year"])
            for r in conn.execute(
                "SELECT year FROM measurement WHERE tree_no=? AND core_no=?",
                (core.tree_no, core.core_no),
            ).fetchall()
        }
        years_for_tree = tree_years.setdefault(core.tree_no, set())
        years_for_tree.update(old_years)
        years_for_tree.update(core.years.keys())
        affected_years.update(old_years)
        affected_years.update(core.years.keys())

        # Upsert the core row first so the measurements can reference it.
        row = conn.execute(
            "SELECT revision FROM core WHERE tree_no=? AND core_no=?",
            (core.tree_no, core.core_no),
        ).fetchone()
        revision = (int(row["revision"]) + 1) if row is not None else 1
        conn.execute(
            "INSERT INTO core(tree_no, core_no, revision, updated_at, data_version) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(tree_no, core_no) DO UPDATE SET "
            "revision=excluded.revision, updated_at=excluded.updated_at, "
            "data_version=excluded.data_version",
            (core.tree_no, core.core_no, revision, now, new_version),
        )

        # Whole-series replacement: the old series is discarded, including
        # years that the new report omits.
        conn.execute(
            "DELETE FROM measurement WHERE tree_no=? AND core_no=?",
            (core.tree_no, core.core_no),
        )
        conn.executemany(
            "INSERT INTO measurement(tree_no, core_no, year, index_value) "
            "VALUES(?,?,?,?)",
            [
                (core.tree_no, core.core_no, year, idx)
                for year, idx in sorted(core.years.items())
            ],
        )
        measurements_written += len(core.years)

    # Only the touched trees can change in the affected years; everything else
    # in tree_year is already current.
    for tree_no in sorted(touched_trees):
        for year in sorted(tree_years[tree_no]):
            _rebuild_tree_year(conn, tree_no, year)

    for year in sorted(affected_years):
        _rebuild_year(conn, year)

    _set_meta_int(conn, "data_version", new_version)

    return {
        "data_version": new_version,
        "trees_written": len(touched_trees),
        "cores_written": len(request.cores),
        "years_changed": len(affected_years),
        "measurements_written": measurements_written,
    }


def apply_report(db_path: str, request: ReportRequest) -> dict[str, int]:
    """Apply one report atomically. Any failure rolls the whole report back."""

    def _run() -> dict[str, int]:
        conn = connect_write(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _apply_report_tx(conn, request)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    return busy_retry(_run)


# ---------------------------------------------------------------------------
# Query path
# ---------------------------------------------------------------------------


def _slot_row(
    session: ReadSession, year: int, slot: int
) -> sqlite3.Row | None:
    session.metrics.slot_probes += 1
    return session.fetchone(
        "SELECT slot, value_x2, cnt, cum FROM year_value_cum "
        "WHERE year=? AND slot=?",
        (year, slot),
    )


def _corrected_cum(row: sqlite3.Row, excluded_value_x2: int | None) -> int:
    cum = int(row["cum"])
    if excluded_value_x2 is not None and excluded_value_x2 <= int(row["value_x2"]):
        cum -= 1
    return cum


def _kth_value_x2(
    session: ReadSession,
    year: int,
    slot_count: int,
    k: int,
    excluded_value_x2: int | None,
    *,
    lo_slot: int = 1,
    lo_row: sqlite3.Row | None = None,
) -> tuple[int, int, sqlite3.Row]:
    """Select the k-th smallest tree median (doubled) after one exclusion.

    ``lo_slot``/``lo_row`` let the caller reuse a row it already read (the
    previous order statistic is never below the previous result's slot).
    Returns ``(value_x2, slot, row)``; only O(log slot_count) rows are read and
    every lookup is a primary-key seek.
    """
    lo, hi = lo_slot, slot_count
    if lo_row is None:
        lo_row = _slot_row(session, year, lo)
        assert lo_row is not None
    cached_lo_row = lo_row
    while lo < hi:
        mid = (lo + hi) // 2
        row = _slot_row(session, year, mid)
        assert row is not None
        if _corrected_cum(row, excluded_value_x2) >= k:
            hi = mid
        else:
            lo = mid + 1
    row = cached_lo_row if lo == lo_slot else _slot_row(session, year, lo)
    assert row is not None
    return int(row["value_x2"]), lo, row


def query_reference(db_path: str, year: int, exclude_tree: int | None) -> dict:
    """Compute one year's reference value, optionally excluding one tree."""
    with read_snapshot(db_path, restricted=True) as session:
        stat = session.fetchone(
            "SELECT tree_count, slot_count FROM year_stat WHERE year=?",
            (year,),
        )
        tree_count_in_year = int(stat["tree_count"]) if stat is not None else 0
        slot_count = int(stat["slot_count"]) if stat is not None else 0

        excluded_value_x2: int | None = None
        excluded_present = False
        if exclude_tree is not None:
            tree_row = session.fetchone(
                "SELECT median_x2 FROM tree_year WHERE year=? AND tree_no=?",
                (year, exclude_tree),
            )
            if tree_row is not None:
                excluded_present = True
                excluded_value_x2 = int(tree_row["median_x2"])

        sample_trees = tree_count_in_year - (1 if excluded_present else 0)
        metrics = session.metrics

        value_x4: int | None = None
        value: float | None = None
        enough = sample_trees >= MIN_TREES and slot_count > 0

        if enough:
            if sample_trees % 2 == 1:
                k = sample_trees // 2 + 1
                v1, slot1, row1 = _kth_value_x2(
                    session, year, slot_count, k, excluded_value_x2
                )
                value_x4 = 2 * v1
            else:
                k_low = sample_trees // 2
                v1, slot1, row1 = _kth_value_x2(
                    session, year, slot_count, k_low, excluded_value_x2
                )
                v2, slot2, row2 = _kth_value_x2(
                    session,
                    year,
                    slot_count,
                    k_low + 1,
                    excluded_value_x2,
                    lo_slot=slot1,
                    lo_row=row1,
                )
                value_x4 = v1 + v2
            value = value_x4 / 4.0

        version = session.fetchone(
            "SELECT value FROM meta WHERE key='data_version'"
        )
        data_version = int(version["value"]) if version is not None else 0

    return {
        "year": year,
        "exclude_tree": exclude_tree,
        "sample_trees": sample_trees,
        "trees_in_year": tree_count_in_year,
        "enough_samples": enough,
        "status": "ok" if enough else "insufficient_samples",
        "min_trees": MIN_TREES,
        "value_x4": value_x4,
        "value": value,
        "excluded_tree_present": excluded_present,
        "excluded_tree_median_x2": excluded_value_x2,
        "data_version": data_version,
        "read_cost": metrics.as_dict(),
    }


# ---------------------------------------------------------------------------
# Administrative read helpers
# ---------------------------------------------------------------------------


def list_trees(db_path: str) -> list[int]:
    with read_snapshot(db_path, restricted=False) as session:
        rows = session.fetchall("SELECT DISTINCT tree_no FROM core ORDER BY tree_no")
        return [int(r["tree_no"]) for r in rows]


def get_core_series(db_path: str, tree_no: int, core_no: int) -> dict | None:
    with read_snapshot(db_path, restricted=False) as session:
        core = session.fetchone(
            "SELECT tree_no, core_no, revision, updated_at FROM core "
            "WHERE tree_no=? AND core_no=?",
            (tree_no, core_no),
        )
        if core is None:
            return None
        rows = session.fetchall(
            "SELECT year, index_value FROM measurement "
            "WHERE tree_no=? AND core_no=? ORDER BY year",
            (tree_no, core_no),
        )
        return {
            "tree_no": int(core["tree_no"]),
            "core_no": int(core["core_no"]),
            "revision": int(core["revision"]),
            "updated_at": core["updated_at"],
            "years": {int(r["year"]): int(r["index_value"]) for r in rows},
        }
