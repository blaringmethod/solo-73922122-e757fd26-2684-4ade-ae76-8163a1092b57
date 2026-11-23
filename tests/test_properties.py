from __future__ import annotations

import math
import random
import statistics

import pytest


def expected_rows_bound(sample_trees: int) -> int:
    """Loose upper bound for rows read by one reference query."""
    return 4 * int(math.ceil(math.log2(max(sample_trees, 1) + 1))) + 12


def post_report(client, cores):
    r = client.post("/reports", json={"cores": cores})
    assert r.status_code == 201, r.text


def get(client, year, exclude=None):
    params = {} if exclude is None else {"exclude_tree": exclude}
    r = client.get(f"/chronology/{year}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def brute_force(series, year, exclude=None):
    """series: {(tree, core): {year: value}}"""
    per_tree = {}
    for (tree, core), years in series.items():
        if tree == exclude or year not in years:
            continue
        per_tree.setdefault(tree, []).append(years[year])
    values = [statistics.median(v) for v in per_tree.values()]
    n = len(values)
    if n < 3:
        return n, None
    return n, statistics.median(values)


@pytest.mark.parametrize("seed", range(4))
def test_randomized_against_bruteforce(client, seed):
    rnd = random.Random(seed)
    series: dict[tuple[int, int], dict[int, int]] = {}
    for _ in range(6):
        batch = []
        for _ in range(rnd.randint(1, 4)):
            tree = rnd.randint(1, 30)
            core_no = rnd.randint(1, 4)
            years = {}
            for year in range(1990, 1998):
                if rnd.random() < 0.75:
                    years[year] = rnd.randint(0, 50)
            series[(tree, core_no)] = years
            batch = [
                {"tree_no": t, "core_no": c, "years": {str(k): v for k, v in ys.items()}}
                for (t, c), ys in series.items()
            ]
        post_report(client, batch)

    # Re-report some cores with a shorter series (whole-core replacement).
    for _ in range(4):
        key = rnd.choice(list(series))
        years = {y: rnd.randint(0, 50) for y in range(1992, 1996) if rnd.random() < 0.7}
        series[key] = years
        post_report(
            client,
            [{"tree_no": key[0], "core_no": key[1], "years": {str(k): v for k, v in years.items()}}],
        )

    trees = sorted({t for t, _ in series})
    for year in range(1988, 2000):
        base = get(client, year)
        n, val = brute_force(series, year)
        assert base["sample_trees"] == n, (seed, year, base, n)
        assert base["value"] == val
        assert base["enough_samples"] == (n >= 3)
        for exclude in [None, *trees, 999]:
            body = get(client, year, exclude)
            n2, val2 = brute_force(series, year, exclude)
            assert body["sample_trees"] == n2
            assert body["value"] == val2
            assert body["status"] == ("ok" if n2 >= 3 else "insufficient_samples")
            if val2 is not None:
                assert body["value_x4"] == int(round(val2 * 4))
                assert body["value_x4"] % 1 == 0


def test_read_cost_sublinear_and_aggregate_only(client, mods):
    chronology = mods["chronology"]
    db = mods["db"]
    path = mods["path"]

    # Ten thousand trees, all with the same median in 2020: many trees, one slot.
    n = 10000
    # Use direct ingest (in batches) to keep the test fast.
    from app.models import ReportRequest

    for start in range(1, n + 1, 5000):
        chronology.apply_report(
            path,
            ReportRequest(
                cores=[
                    {"tree_no": t, "core_no": 1, "years": {"2020": 100}}
                    for t in range(start, min(start + 5000, n + 1))
                ]
            ),
        )
    result = chronology.query_reference(path, 2020, 1)
    assert result["sample_trees"] == n - 1
    assert result["value"] == 100.0
    assert result["read_cost"]["rows_read"] <= expected_rows_bound(n)
    assert result["read_cost"]["slot_probes"] <= 2

    # Distinct medians: rows read still only logarithmic.
    cores = [
        {"tree_no": t, "core_no": 1, "years": {"2021": t}}
        for t in range(1, 5001)
    ]
    chronology.apply_report(path, ReportRequest(cores=cores))
    result = chronology.query_reference(path, 2021, 999_999)
    assert result["sample_trees"] == 5000
    assert result["read_cost"]["rows_read"] <= expected_rows_bound(5000)
    assert result["read_cost"]["rows_read"] < 5000 / 10


def test_query_plan_uses_only_seeks(client, mods):
    chronology = mods["chronology"]
    from app.models import ReportRequest

    cores = [
        {"tree_no": t, "core_no": 1, "years": {"2020": t % 97}}
        for t in range(1, 500)
    ]
    chronology.apply_report(mods["path"], ReportRequest(cores=cores))

    conn = mods["db"].connect_read(mods["path"], restricted=False)
    try:
        plans = {
            "stat": "SELECT tree_count, slot_count FROM year_stat WHERE year=?",
            "tree": "SELECT median_x2 FROM tree_year WHERE year=? AND tree_no=?",
            "slot": "SELECT slot, value_x2, cnt, cum FROM year_value_cum WHERE year=? AND slot=?",
            "version": "SELECT value FROM meta WHERE key=?",
        }
        for name, sql in plans.items():
            if name == "stat":
                params = (2020,)
            elif name == "tree":
                params = (2020, 1)
            elif name == "slot":
                params = (2020, 1)
            else:
                params = ("data_version",)
            rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            detail = " ".join(r["detail"] for r in rows)
            assert "SCAN" not in detail, (name, detail)
            assert "SEARCH" in detail, (name, detail)
    finally:
        conn.close()


def test_reference_connection_cannot_read_raw_measurements(client, mods):
    import sqlite3

    from app.models import ReportRequest

    mods["chronology"].apply_report(
        mods["path"],
        ReportRequest(cores=[{"tree_no": 1, "core_no": 1, "years": {"2020": 7}}]),
    )
    conn = mods["db"].connect_read(mods["path"], restricted=True)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM measurement").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM core").fetchall()
        # Aggregates remain readable.
        assert conn.execute("SELECT COUNT(*) AS n FROM year_stat").fetchone()["n"] == 1
    finally:
        conn.close()


def test_schema_stores_no_per_tree_excluded_answers(client, mods):
    expected = {
        "meta": {"key", "value"},
        "core": {"tree_no", "core_no", "revision", "updated_at", "data_version"},
        "measurement": {"tree_no", "core_no", "year", "index_value"},
        "tree_year": {"year", "tree_no", "median_x2", "core_count"},
        "year_stat": {"year", "tree_count", "slot_count"},
        "year_value_cum": {"year", "slot", "value_x2", "cnt", "cum"},
    }
    conn = mods["db"].connect_read(mods["path"], restricted=False)
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(expected)
        for table, cols in expected.items():
            actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert actual == cols, table
    finally:
        conn.close()
