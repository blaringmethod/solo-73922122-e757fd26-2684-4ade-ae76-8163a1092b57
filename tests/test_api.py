from __future__ import annotations

import math
import sqlite3
import statistics

import pytest


def report(client, cores):
    r = client.post("/reports", json={"cores": cores})
    assert r.status_code == 201, r.text
    return r.json()


def core(tree, cno, years):
    return {"tree_no": tree, "core_no": cno, "years": {str(y): v for y, v in years.items()}}


def get_year(client, year, exclude=None):
    params = {} if exclude is None else {"exclude_tree": exclude}
    r = client.get(f"/chronology/{year}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def expected_value(tree_cores, year, exclude=None):
    per_tree = {}
    for t, cores in tree_cores.items():
        if t == exclude:
            continue
        vals = [c[year] for c in cores if year in c]
        if vals:
            per_tree[t] = statistics.median(vals)
    if len(per_tree) < 3:
        return len(per_tree), None
    return len(per_tree), statistics.median(per_tree.values())


def seed(client):
    # tree 1..5, two cores each; all have year 1950
    tree_cores = {}
    for t in range(1, 6):
        c1 = {1950: 10 * t, 1951: 10 * t + 1}
        c2 = {1950: 10 * t + 2, 1951: 10 * t + 3}
        tree_cores[t] = [c1, c2]
        report(client, [core(t, 1, c1), core(t, 2, c2)])
    return tree_cores


def test_health_and_empty_year(client):
    assert client.get("/health").json()["schema_ok"] is True
    body = get_year(client, 1950)
    assert body["sample_trees"] == 0
    assert body["enough_samples"] is False
    assert body["status"] == "insufficient_samples"
    assert body["value"] is None


def test_median_of_medians_and_exclusion(client):
    tree_cores = seed(client)
    body = get_year(client, 1950)
    n, val = expected_value(tree_cores, 1950)
    assert body["sample_trees"] == n == 5
    assert body["enough_samples"] is True
    assert body["value"] == val

    body2 = get_year(client, 1950, exclude=1)
    n2, val2 = expected_value(tree_cores, 1950, exclude=1)
    assert body2["sample_trees"] == n2 == 4
    assert body2["excluded_tree_present"] is True
    assert body2["value"] == val2
    assert body2["value"] != body["value"]


def test_insufficient_below_three_trees(client):
    tree_cores = seed(client)
    assert get_year(client, 1950, exclude=5)["sample_trees"] == 4
    # remove two more trees from the year via re-report with empty-ish series
    report(client, [core(1, 1, {1951: 1}), core(1, 2, {1951: 1})])
    report(client, [core(2, 1, {1951: 1}), core(2, 2, {1951: 1})])
    body = get_year(client, 1950)
    assert body["sample_trees"] == 3
    assert body["value"] == expected_value({3: tree_cores[3], 4: tree_cores[4], 5: tree_cores[5]}, 1950)[1]
    body2 = get_year(client, 1950, exclude=3)
    assert body2["sample_trees"] == 2
    assert body2["enough_samples"] is False
    assert body2["status"] == "insufficient_samples"
    assert body2["value"] is None


def test_exclude_absent_tree_changes_nothing(client):
    seed(client)
    base = get_year(client, 1950)
    body = get_year(client, 1950, exclude=999)
    assert body["excluded_tree_present"] is False
    assert body["sample_trees"] == base["sample_trees"]
    assert body["value"] == base["value"]


def test_core_year_alignment_uses_only_cores_with_year(client):
    report(client, [core(1, 1, {1960: 10}), core(1, 2, {1961: 90})])
    report(client, [core(2, 1, {1960: 20})])
    report(client, [core(3, 1, {1960: 30})])
    body = get_year(client, 1960)
    assert body["sample_trees"] == 3
    assert body["value"] == 20.0


def test_reingest_replaces_whole_core(client):
    seed(client)
    before = get_year(client, 1951)
    assert before["sample_trees"] == 5
    # tree 1 core 2 loses 1951; tree has core1 1951 = 11, core2 now stops
    report(client, [core(1, 2, {1950: 12})])
    after = get_year(client, 1951)
    assert after["sample_trees"] == 5
    assert after["value"] == 32.0
    # a year present only in the old report disappears
    report(client, [core(1, 1, {1950: 10})])
    body = get_year(client, 1951)
    assert body["sample_trees"] == 4
    assert body["value"] == 37.0


def test_losing_all_cores_removes_tree_from_year(client):
    seed(client)
    report(client, [core(1, 1, {1950: 10}), core(1, 2, {1950: 12})])
    report(client, [core(1, 1, {}), core(1, 2, {})])
    body = get_year(client, 1950)
    assert body["sample_trees"] == 4
    assert client.get("/cores/1/1").json()["years"] == {}


def test_failed_write_leaves_previous_state(client, monkeypatch, mods):
    tree_cores = seed(client)
    before = get_year(client, 1950)
    core_before = client.get("/cores/2/1").json()

    import app.chronology as chronology

    real_rebuild = chronology._rebuild_year

    def boom(conn, year):
        if year == 1950:
            raise sqlite3.OperationalError("simulated failure")
        return real_rebuild(conn, year)

    monkeypatch.setattr(chronology, "_rebuild_year", boom)
    r = client.post(
        "/reports",
        json={"cores": [core(2, 1, {1950: 777})]},
    )
    assert r.status_code == 500
    monkeypatch.setattr(chronology, "_rebuild_year", real_rebuild)

    after = get_year(client, 1950)
    assert after["value"] == before["value"]
    assert after["data_version"] == before["data_version"]
    assert client.get("/cores/2/1").json()["years"] == core_before["years"]
    assert client.get("/cores/2/1").json()["revision"] == core_before["revision"]


def test_validation_errors(client):
    assert client.post("/reports", json={"cores": []}).status_code == 422
    assert client.post(
        "/reports",
        json={"cores": [{"tree_no": 1, "core_no": 1, "years": {"1800": -2}}]},
    ).status_code == 422
    assert client.post(
        "/reports",
        json={"cores": [{"tree_no": 1, "core_no": 1, "years": {"0": 2}}]},
    ).status_code == 422
    assert client.post(
        "/reports",
        json={
            "cores": [
                {"tree_no": 1, "core_no": 1, "years": {"1900": 2}},
                {"tree_no": 1, "core_no": 1, "years": {"1900": 3}},
            ]
        },
    ).status_code == 422
    assert client.get("/chronology/1950?exclude_tree=0").status_code == 422
    assert client.get("/chronology/0").status_code == 422
    assert client.post(
        "/reports",
        json={"cores": [{"tree_no": 1, "core_no": 1, "years": {"1900": True}}]},
    ).status_code == 422
    assert client.post(
        "/reports",
        json={"cores": [{"tree_no": 1, "core_no": 1, "years": {"1900": 5.5}}]},
    ).status_code == 422


def test_even_count_median_half_values(client):
    # even number of trees -> average of the two middle tree medians may be .5
    report(client, [core(1, 1, {2000: 1})])
    report(client, [core(2, 1, {2000: 2})])
    report(client, [core(3, 1, {2000: 4})])
    report(client, [core(4, 1, {2000: 5})])
    body = get_year(client, 2000)
    assert body["sample_trees"] == 4
    assert body["value"] == 3.0
    assert body["value_x4"] == 12
    # inner median of even cores: (1 + 4) / 2 = 2.5 -> x2 = 5
    report(client, [core(1, 2, {2000: 4})])
    body = get_year(client, 2000)
    # tree1 inner median (1,4)=2.5; trees: 2.5, 2, 4, 5 -> median (2.5+4)/2=3.25
    assert body["value"] == pytest.approx(3.25)
    assert body["value_x4"] == 13
