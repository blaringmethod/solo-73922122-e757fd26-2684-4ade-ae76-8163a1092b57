from __future__ import annotations

import random
import sqlite3
import threading
from pathlib import Path

import pytest

from app.models import ReportRequest


def core(tree, cno, years):
    return {"tree_no": tree, "core_no": cno, "years": {str(y): v for y, v in years.items()}}


def post(client, cores):
    r = client.post("/reports", json={"cores": cores})
    assert r.status_code == 201, r.text
    return r.json()


def get_common(client, start, end, min_trees):
    r = client.get(
        "/common-sample",
        params={"start_year": start, "end_year": end, "min_trees": min_trees},
    )
    assert r.status_code in (200, 404), r.text
    return r.status_code, r.json()


def brute_common(series, start_year, end_year, min_trees):
    """Candidate oracle: a tree counts only if it covers *every* year of the
    segment (完整覆盖).  The number of trees covering [s, e] is non-increasing
    in ``e``, so the inner loop stops as soon as it drops below min_trees."""
    by_year: dict[int, set[int]] = {}
    all_trees: set[int] = set()
    for (tree, _core), years in series.items():
        all_trees.add(tree)
        for year in years:
            by_year.setdefault(year, set()).add(tree)

    best = None
    for s in range(start_year, end_year + 1):
        for e in range(s, end_year + 1):
            covering = [
                t
                for t in all_trees
                if all(t in by_year.get(y, ()) for y in range(s, e + 1))
            ]
            if len(covering) >= min_trees:
                length = e - s + 1
                if best is None or length > best[0]:
                    best = (length, s, e, sorted(covering))
            else:
                break
    return best


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_basic_longest_segment_and_trees(client):
    # Trees 1..4 cover 1950..1954; tree 5 covers only 1950..1952.
    for t in range(1, 5):
        post(client, [core(t, 1, {y: 10 * t for y in range(1950, 1955)})])
    post(client, [core(5, 1, {y: 50 for y in range(1950, 1953)})])

    status, body = get_common(client, 1950, 1954, 3)
    assert status == 200
    assert body["status"] == "ok" and body["found"] is True
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (1950, 1954, 5)
    assert body["tree_count"] == 4
    assert body["trees"] == [1, 2, 3, 4]
    assert body["data_version"] >= 1

    # Requiring all five trees truncates the segment to 1950..1952.
    status, body = get_common(client, 1950, 1954, 5)
    assert (body["segment_start"], body["segment_end"]) == (1950, 1952)
    assert body["trees"] == [1, 2, 3, 4, 5]


def test_tie_breaks_to_earlier_start_and_window_is_inclusive(client):
    # Two equally long 2-year segments: 1960..1961 and 1970..1971.
    for t in range(1, 4):
        post(
            client,
            [
                core(t, 1, {1960: 1, 1961: 1}),
                core(t, 2, {1970: 1, 1971: 1}),
            ],
        )
    status, body = get_common(client, 1955, 1980, 3)
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (1960, 1961, 2)

    # Windows are inclusive on both ends: querying exactly 1960..1960 works.
    status, body = get_common(client, 1960, 1960, 3)
    assert status == 200
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (1960, 1960, 1)


def test_zero_values_count_as_coverage(client):
    # Zeros are real reported values, so they must keep the run alive.
    for t in range(1, 4):
        post(client, [core(t, 1, {1900: 0, 1901: 0, 1902: 0})])
    status, body = get_common(client, 1900, 1902, 3)
    assert status == 200
    assert (body["segment_start"], body["segment_end"]) == (1900, 1902)
    assert body["trees"] == [1, 2, 3]


def test_one_core_among_several_keeps_tree_in_run(client):
    # Tree 1: core 1 covers 1900..1904, core 2 only 1903. The tree is still
    # covered for the whole 1900..1904 run.
    post(client, [core(1, 1, {y: 5 for y in range(1900, 1905)})])
    post(client, [core(1, 2, {1903: 7})])
    for t in (2, 3):
        post(client, [core(t, 1, {y: 5 for y in range(1900, 1905)})])
    status, body = get_common(client, 1900, 1904, 3)
    assert body["trees"] == [1, 2, 3]
    assert (body["segment_start"], body["segment_end"]) == (1900, 1904)


def test_no_solution_is_explicit(client):
    post(client, [core(1, 1, {2000: 1})])
    post(client, [core(2, 1, {2010: 1})])
    status, body = get_common(client, 1995, 2015, 2)
    assert status == 404
    assert body["status"] == "no_solution"
    assert body["found"] is False
    assert body["segment_start"] is None and body["segment_end"] is None
    assert body["trees"] == []
    assert body["data_version"] >= 1


def test_min_trees_one_uses_single_tree_run(client):
    post(client, [core(7, 1, {1985: 1, 1986: 1, 1988: 1})])
    status, body = get_common(client, 1980, 1990, 1)
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (1985, 1986, 2)
    assert body["trees"] == [7]


def test_reported_core_dropping_a_year_splits_the_run(client):
    # Re-report a whole core without the bridging year: run must split, and a
    # less-covered tree becomes the limiting factor.
    for t in range(1, 4):
        post(client, [core(t, 1, {1950: 1, 1951: 1, 1952: 1})])
    status, body = get_common(client, 1950, 1952, 3)
    assert (body["segment_start"], body["segment_end"]) == (1950, 1952)

    # Tree 3 loses 1951 (whole-core replacement). Now only 1950 and 1952 have
    # three trees, so the longest 3-tree segment is one year; the earlier
    # start (1950) wins the tie.
    post(client, [core(3, 1, {1950: 1, 1952: 1})])
    status, body = get_common(client, 1950, 1952, 3)
    assert status == 200
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (1950, 1950, 1)
    assert body["trees"] == [1, 2, 3]

    # With two trees the whole window is still covered, by trees 1 and 2
    # (tree 3 does not cover 1951, so it cannot be listed).
    status, body = get_common(client, 1950, 1952, 2)
    assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (
        1950,
        1952,
        3,
    )
    assert body["trees"] == [1, 2]


def test_reraising_other_trees_does_not_disturb_spans(mods):
    from app.chronology import apply_report, query_common_sample

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {y: 1 for y in range(1900, 1911)}}
                for t in range(1, 4)
            ]
        ),
    )
    before = query_common_sample(path, 1900, 1910, 3)
    # Re-report an unrelated tree far away: the answer must be unchanged.
    apply_report(
        path,
        ReportRequest(cores=[{"tree_no": 99, "core_no": 1, "years": {"2500": 3}}]),
    )
    after = query_common_sample(path, 1900, 1910, 3)
    assert after["segment_start"] == before["segment_start"]
    assert after["trees"] == before["trees"]
    assert after["data_version"] > before["data_version"]


# ---------------------------------------------------------------------------
# Randomized cross-check against brute force
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_randomized_against_bruteforce(client, seed):
    rnd = random.Random(seed)
    series: dict[tuple[int, int], dict[int, int]] = {}
    for _ in range(8):
        for _ in range(rnd.randint(1, 5)):
            tree = rnd.randint(1, 12)
            cno = rnd.randint(1, 3)
            years = {
                y: rnd.randint(0, 30)
                for y in range(1900, 1915)
                if rnd.random() < 0.7
            }
            series[(tree, cno)] = years
        post(client, [core(t, c, ys) for (t, c), ys in series.items()])

    for _ in range(6):
        start = rnd.randint(1895, 1910)
        end = rnd.randint(start, 1918)
        min_trees = rnd.randint(1, 6)
        expected = brute_common(series, start, end, min_trees)
        status, body = get_common(client, start, end, min_trees)
        if expected is None:
            assert status == 404, (seed, start, end, min_trees, body)
            assert body["found"] is False
        else:
            length, s, e, trees = expected
            assert status == 200, (seed, start, end, min_trees, body)
            assert (body["segment_start"], body["segment_end"], body["segment_length"]) == (
                s,
                e,
                length,
            ), (seed, start, end, min_trees, expected, body)
            assert body["trees"] == trees
            assert body["tree_count"] == len(trees) >= min_trees


# ---------------------------------------------------------------------------
# Atomicity, snapshots, upgrade, cost
# ---------------------------------------------------------------------------


def test_span_index_rolls_back_with_the_report(mods, monkeypatch):
    from app.chronology import apply_report, query_common_sample

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {y: 1 for y in (1950, 1951)}}
                for t in (1, 2, 3)
            ]
        ),
    )
    before = query_common_sample(path, 1950, 1951, 3)

    real = mods["chronology"]._rebuild_tree_span

    def boom(conn, tree_no):
        raise sqlite3.OperationalError("simulated failure while rebuilding spans")

    monkeypatch.setattr(mods["chronology"], "_rebuild_tree_span", boom)
    with pytest.raises(sqlite3.OperationalError):
        apply_report(
            path,
            ReportRequest(cores=[{"tree_no": 1, "core_no": 1, "years": {"1950": 9}}]),
        )
    monkeypatch.setattr(mods["chronology"], "_rebuild_tree_span", real)

    after = query_common_sample(path, 1950, 1951, 3)
    assert after["segment_start"] == before["segment_start"]
    assert after["trees"] == before["trees"]
    assert after["data_version"] == before["data_version"]

    # And the index still agrees with a fresh recomputation for every tree.
    with mods["db"].read_snapshot(path, restricted=False) as session:
        for tree in (1, 2, 3):
            years = [
                int(r["year"])
                for r in session.fetchall(
                    "SELECT year FROM tree_year WHERE tree_no=? ORDER BY year", (tree,)
                )
            ]
            spans = [
                (int(r["start_year"]), int(r["end_year"]))
                for r in session.fetchall(
                    "SELECT start_year, end_year FROM tree_span WHERE tree_no=? "
                    "ORDER BY start_year",
                    (tree,),
                )
            ]
            assert [y for a, b in spans for y in range(a, b + 1)] == years


def test_query_sees_one_committed_snapshot_during_writes(mods):
    from app.chronology import apply_report, query_common_sample
    from app.db import read_snapshot

    path = mods["path"]
    for t in range(1, 21):
        apply_report(
            path,
            ReportRequest(
                cores=[{"tree_no": t, "core_no": 1, "years": {1990: 5, 1991: 5}}]
            ),
        )

    stop = threading.Event()
    errors: list[BaseException] = []
    observed: list[dict] = []

    def writer():
        try:
            n = 0
            while not stop.is_set() and n < 60:
                # Every few writes tree 1 drops 1991, then regains it. With
                # min_trees=20 that toggles the covered segment between
                # 1990..1991 and 1990..1990.
                years = {1990: 5} if (n // 3) % 2 else {1990: 5, 1991: 5}
                apply_report(
                    path,
                    ReportRequest(
                        cores=[{"tree_no": 1, "core_no": 1, "years": years}]
                    ),
                )
                n += 1
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                observed.append(query_common_sample(path, 1990, 1991, 20))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    wt.join()
    stop.set()
    rt.join()

    assert not errors, errors
    assert observed

    # The writer toggles tree 1's 1991 coverage in blocks of three writes.
    # Seed wrote versions 1..20 (tree 1 first, so every version >= 20 has all
    # 20 trees on 1990). Writer write n lands on version 21 + n and keeps 1991
    # iff (n // 3) % 2 == 0. The answer is therefore determined by the version,
    # and every observation must match it exactly -- segment, tree list and
    # version all from one committed state.
    for result in observed:
        v = result["data_version"]
        assert v >= 20
        has_1991 = v == 20 or ((v - 21) // 3) % 2 == 0
        if has_1991:
            assert (result["segment_start"], result["segment_end"]) == (1990, 1991)
            assert result["trees"] == list(range(1, 21))
        else:
            assert (result["segment_start"], result["segment_end"]) == (1990, 1990)
            assert result["trees"] == list(range(1, 21))
        assert len(result["trees"]) >= 20


def test_snapshot_version_matches_returned_trees(mods):
    from app.chronology import apply_report, query_common_sample
    from app.db import read_data_version

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {2000: 1, 2001: 1}}
                for t in (1, 2, 3)
            ]
        ),
    )
    result = query_common_sample(path, 2000, 2001, 3)
    assert result["data_version"] == read_data_version(path)
    assert result["trees"] == [1, 2, 3]


def test_legacy_volume_without_span_index_is_upgraded(tmp_path, client):
    """Simulate a pre-existing data volume: full old schema, no tree_span."""
    import app.db as db
    import app.main as main_mod
    import app.settings as settings_mod

    old_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(old_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO meta(key, value) VALUES ('data_version', '7');
        CREATE TABLE core (
            tree_no INTEGER NOT NULL, core_no INTEGER NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
            data_version INTEGER NOT NULL, PRIMARY KEY (tree_no, core_no)
        ) WITHOUT ROWID;
        CREATE TABLE measurement (
            tree_no INTEGER NOT NULL, core_no INTEGER NOT NULL,
            year INTEGER NOT NULL, index_value INTEGER NOT NULL,
            PRIMARY KEY (tree_no, core_no, year)
        ) WITHOUT ROWID;
        CREATE TABLE tree_year (
            year INTEGER NOT NULL, tree_no INTEGER NOT NULL,
            median_x2 INTEGER NOT NULL, core_count INTEGER NOT NULL,
            PRIMARY KEY (year, tree_no)
        ) WITHOUT ROWID;
        CREATE TABLE year_stat (
            year INTEGER PRIMARY KEY, tree_count INTEGER NOT NULL,
            slot_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE year_value_cum (
            year INTEGER NOT NULL, slot INTEGER NOT NULL,
            value_x2 INTEGER NOT NULL, cnt INTEGER NOT NULL, cum INTEGER NOT NULL,
            PRIMARY KEY (year, slot)
        ) WITHOUT ROWID;
        """
    )
    # Three trees covering 1970..1974, one tree only 1970..1971.
    for tree in (1, 2, 3):
        conn.execute(
            "INSERT INTO core VALUES (?,1,1,'2020-01-01T00:00:00+00:00',7)", (tree,)
        )
        conn.executemany(
            "INSERT INTO measurement VALUES (?,1,?,100)",
            [(tree, y) for y in range(1970, 1975)],
        )
        conn.executemany(
            "INSERT INTO tree_year VALUES (?,?,200,1)",
            [(y, tree) for y in range(1970, 1975)],
        )
    conn.execute("INSERT INTO core VALUES (4,1,1,'2020-01-01T00:00:00+00:00',7)")
    conn.executemany(
        "INSERT INTO measurement VALUES (4,1,?,100)", [(y,) for y in (1970, 1971)]
    )
    conn.executemany(
        "INSERT INTO tree_year VALUES (?,4,200,1)", [(y,) for y in (1970, 1971)]
    )
    for y in range(1970, 1975):
        conn.execute("INSERT INTO year_stat VALUES (?,?,?)", (y, 3, 1))
        conn.execute("INSERT INTO year_value_cum VALUES (?,?,?,?,?)", (y, 1, 200, 3, 3))
    conn.commit()
    conn.close()

    # Restart against the legacy volume: init_db must backfill tree_span.
    main_mod.settings = settings_mod.Settings(db_path=old_path)
    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as legacy_client:
        r = legacy_client.get(
            "/common-sample",
            params={"start_year": 1965, "end_year": 1980, "min_trees": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["segment_start"], body["segment_end"]) == (1970, 1974)
        assert body["trees"] == [1, 2, 3]

        r = legacy_client.get(
            "/common-sample",
            params={"start_year": 1965, "end_year": 1980, "min_trees": 4},
        )
        assert (r.json()["segment_start"], r.json()["segment_end"]) == (1970, 1971)
        assert r.json()["trees"] == [1, 2, 3, 4]

        # The upgraded volume keeps accepting writes and its new spans follow.
        legacy_client.post(
            "/reports",
            json={"cores": [core(4, 1, {y: 100 for y in range(1970, 1975)})]},
        )
        r = legacy_client.get(
            "/common-sample",
            params={"start_year": 1965, "end_year": 1980, "min_trees": 4},
        )
        assert (r.json()["segment_start"], r.json()["segment_end"]) == (1970, 1974)
        assert r.json()["trees"] == [1, 2, 3, 4]


def test_common_sample_connection_cannot_read_per_year_or_raw_tables(mods):
    from app.chronology import apply_report
    from app.db import connect_read

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(cores=[{"tree_no": 1, "core_no": 1, "years": {"2000": 1}}]),
    )
    conn = connect_read(path, span_only=True)
    try:
        for table in ("tree_year", "year_stat", "year_value_cum", "measurement", "core"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"SELECT * FROM {table}").fetchall()
        assert conn.execute("SELECT COUNT(*) AS n FROM tree_span").fetchone()["n"] >= 1
        assert conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
    finally:
        conn.close()


def test_span_index_query_plans_never_scan(mods):
    conn = mods["db"].connect_read(mods["path"], restricted=False)
    try:
        # The sweep is one streamed, *covering-index* traversal of the interval
        # index in key order; no table heap, no per-year expansion, no sort.
        sweep = (
            "SELECT start_year, end_year FROM tree_span "
            "ORDER BY start_year, end_year"
        )
        detail = " ".join(
            r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sweep)
        )
        assert "USING COVERING INDEX tree_span_by_start" in detail, detail
        assert "TEMP B-TREE" not in detail, detail

        # Resolving the winning segment to tree numbers is a range search.
        stab = (
            "SELECT tree_no FROM tree_span INDEXED BY tree_span_by_end "
            "WHERE start_year <= ? AND end_year >= ? ORDER BY tree_no"
        )
        detail = " ".join(
            r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + stab, (1, 1))
        )
        assert "SEARCH" in detail, detail
        assert "SCAN" not in detail, detail
    finally:
        conn.close()


def test_span_index_has_no_per_year_expansion(mods):
    """Many years, few trees => the index stores runs, not one row per year."""
    from app.chronology import apply_report

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {y: 1 for y in range(1000, 3000)}}
                for t in (1, 2, 3)
            ]
        ),
    )
    with mods["db"].read_snapshot(path, restricted=False) as session:
        rows = session.fetchone("SELECT COUNT(*) AS n FROM tree_span")["n"]
        years = session.fetchone(
            "SELECT COUNT(*) AS n FROM tree_year"
        )["n"]
    assert years == 3 * 2000
    assert rows == 3  # one maximal run per tree


def test_read_cost_depends_on_intervals_and_min_trees(mods):
    import tracemalloc

    from app.chronology import apply_report, query_common_sample

    path = mods["path"]
    # 200 trees, each with 40 separate 1-year runs => 8000 interval rows, and
    # 40 candidate years. Query cost must track intervals, not years * trees.
    cores = []
    for t in range(1, 201):
        years = {y: 1 for y in range(1000, 1200, 5)}  # 40 isolated years
        cores.append({"tree_no": t, "core_no": 1, "years": years})
    for start in range(0, len(cores), 50):
        apply_report(path, ReportRequest(cores=cores[start : start + 50]))

    result = query_common_sample(path, 900, 1300, 100)
    assert result["trees"] and len(result["trees"]) == 200
    # rows read: at most one row per interval plus the final stabbing result.
    assert result["read_cost"]["rows_read"] <= 8000 + 200 + 4

    # Memory must scale with min_trees, not with the number of intervals.
    tracemalloc.start()
    query_common_sample(path, 900, 1300, 1)
    _, peak_small = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    query_common_sample(path, 900, 1300, 5000)
    _, peak_large = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # A 5000-entry heap is larger than a 1-entry heap but still tiny; and the
    # 8000-row stream is never materialised into a list (that would dominate).
    assert peak_large < 4 * 1024 * 1024
    assert peak_large >= peak_small


def test_validation_errors(client):
    assert client.get("/common-sample?start_year=2000&end_year=1999&min_trees=1").status_code == 422
    assert client.get("/common-sample?start_year=0&end_year=1999&min_trees=1").status_code == 422
    assert client.get("/common-sample?start_year=2000&end_year=2001&min_trees=0").status_code == 422
    assert (
        client.post(
            "/common-sample",
            json={"start_year": 2000, "end_year": 1999, "min_trees": 1},
        ).status_code
        == 422
    )


def test_reingest_only_rewrites_affected_trees_spans(mods, monkeypatch):
    """A report for tree 2 must not delete/rebuild spans of untouched trees."""
    from app.chronology import apply_report, _rebuild_tree_span

    path = mods["path"]
    apply_report(
        path,
        ReportRequest(
            cores=[
                {"tree_no": t, "core_no": 1, "years": {y: 1 for y in (1980, 1981)}}
                for t in (1, 2, 3)
            ]
        ),
    )

    calls: list[int] = []
    real = _rebuild_tree_span

    def spy(conn, tree_no):
        calls.append(tree_no)
        return real(conn, tree_no)

    monkeypatch.setattr(mods["chronology"], "_rebuild_tree_span", spy)
    apply_report(
        path,
        ReportRequest(cores=[{"tree_no": 2, "core_no": 1, "years": {"1980": 1}}]),
    )
    assert calls == [2]

    # Untouched trees keep their original two-year runs.
    with mods["db"].read_snapshot(path, restricted=False) as session:
        for tree in (1, 3):
            spans = [
                (int(r["start_year"]), int(r["end_year"]))
                for r in session.fetchall(
                    "SELECT start_year, end_year FROM tree_span WHERE tree_no=?",
                    (tree,),
                )
            ]
            assert spans == [(1980, 1981)]
        spans2 = [
            (int(r["start_year"]), int(r["end_year"]))
            for r in session.fetchall(
                "SELECT start_year, end_year FROM tree_span WHERE tree_no=2"
            )
        ]
        assert spans2 == [(1980, 1980)]
