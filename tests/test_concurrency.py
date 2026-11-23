from __future__ import annotations

import sqlite3
import threading
import time

import pytest


def test_report_rolls_back_on_injected_failure(mods, monkeypatch):
    chronology = mods["chronology"]
    path = mods["path"]

    from app.chronology import apply_report
    from app.models import ReportRequest

    apply_report(
        path,
        ReportRequest(cores=[{"tree_no": 1, "core_no": 1, "years": {"2000": 10}}]),
    )
    before = chronology.query_reference(path, 2000, None)

    real = chronology._rebuild_year

    def boom(conn, year):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(chronology, "_rebuild_year", boom)
    with pytest.raises(sqlite3.OperationalError):
        apply_report(
            path,
            ReportRequest(cores=[{"tree_no": 1, "core_no": 1, "years": {"2000": 99}}]),
        )
    monkeypatch.setattr(chronology, "_rebuild_year", real)

    after = chronology.query_reference(path, 2000, None)
    assert after["value"] == before["value"]
    assert after["data_version"] == before["data_version"]
    series = chronology.get_core_series(path, 1, 1)
    assert series["years"] == {2000: 10}
    assert series["revision"] == 1


def test_concurrent_reports_serialize_without_corruption(mods):
    from app.chronology import apply_report
    from app.models import ReportRequest

    path = mods["path"]
    errors: list[BaseException] = []

    def worker(offset: int):
        try:
            for i in range(5):
                apply_report(
                    path,
                    ReportRequest(
                        cores=[
                            {
                                "tree_no": offset * 100 + j,
                                "core_no": 1,
                                "years": {2000 + i: offset * 100 + j},
                            }
                            for j in range(1, 6)
                        ]
                    ),
                )
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    conn = mods["db"].connect_read(path, restricted=False)
    try:
        # Every tree/year aggregate must agree with the raw measurements.
        raw_rows = conn.execute(
            "SELECT tree_no, year, index_value FROM measurement ORDER BY tree_no, year, index_value"
        ).fetchall()
        by_group: dict[tuple[int, int], list[int]] = {}
        for r in raw_rows:
            by_group.setdefault((r["tree_no"], r["year"]), []).append(r["index_value"])
        stored = {
            (r["tree_no"], r["year"]): r["median_x2"]
            for r in conn.execute("SELECT tree_no, year, median_x2 FROM tree_year")
        }
        assert set(stored) == set(by_group)
        for key, vals in by_group.items():
            n = len(vals)
            expected = 2 * vals[n // 2] if n % 2 else vals[n // 2 - 1] + vals[n // 2]
            assert stored[key] == expected, (key, stored[key], expected)
        stats = conn.execute(
            "SELECT COUNT(*) AS bad FROM year_stat s WHERE s.tree_count != "
            "(SELECT COUNT(*) FROM tree_year t WHERE t.year = s.year)"
        ).fetchone()["bad"]
        assert stats == 0
    finally:
        conn.close()


def test_reader_sees_single_version_during_writes(mods):
    from app.chronology import apply_report
    from app.models import ReportRequest

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {"1980": 100 + t}}
                for t in range(1, 51)
            ]
        ),
    )

    stop = threading.Event()
    seen_versions: set[tuple[int, int]] = set()
    errors: list[BaseException] = []

    def writer():
        try:
            n = 0
            while not stop.is_set() and n < 40:
                apply_report(
                    path,
                    ReportRequest(
                        cores=[
                            {"tree_no": 1, "core_no": 1, "years": {"1980": 100 + n}}
                        ]
                    ),
                )
                n += 1
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                with mods["db"].read_snapshot(path, restricted=True) as session:
                    v = int(session.fetchone("SELECT value FROM meta WHERE key='data_version'")["value"])
                    stat = session.fetchone(
                        "SELECT tree_count FROM year_stat WHERE year=1980"
                    )
                    seen_versions.add((v, int(stat["tree_count"])))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start(); rt.start()
    wt.join()
    stop.set()
    rt.join()

    assert not errors, errors
    # The tree count never changes (all reports touch tree 1), so consistency
    # of the pair (version, count) is the observable invariant.
    assert all(count == 50 for _, count in seen_versions)

    # Every observed (data_version, aggregate triplet) must be internally
    # consistent: slots' counts sum to the year's tree count and to the number
    # of tree_year rows, i.e. no half-committed write was ever visible.
    with mods["db"].read_snapshot(path, restricted=False) as session:
        a = session.fetchone(
            "SELECT SUM(cnt) AS s FROM year_value_cum WHERE year=1980"
        )["s"]
        b = session.fetchone("SELECT tree_count FROM year_stat WHERE year=1980")["tree_count"]
        c = session.fetchone(
            "SELECT COUNT(*) AS n FROM tree_year WHERE year=1980"
        )["n"]
        assert a == b == c == 50
