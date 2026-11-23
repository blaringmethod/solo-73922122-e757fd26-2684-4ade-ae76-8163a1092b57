#!/usr/bin/env python3
"""Seed a tiny stand, query a year with/without one tree, re-report, query again.

Usage:
    python scripts/demo.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def core(tree: int, cno: int, years: dict[int, int]) -> dict:
    return {"tree_no": tree, "core_no": cno, "years": {str(y): v for y, v in years.items()}}


def show(label: str, body: dict) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> int:
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    seed = {
        "cores": [
            core(1, 1, {1975: 90, 1976: 92}),
            core(1, 2, {1975: 96, 1976: 94}),
            core(2, 1, {1975: 104, 1976: 100}),
            core(2, 2, {1975: 110, 1976: 102}),
            core(3, 1, {1975: 121, 1976: 108}),
            core(3, 2, {1975: 127, 1976: 110}),
            core(4, 1, {1975: 140, 1976: 118}),
            core(5, 1, {1975: 160, 1976: 126}),
        ]
    }
    status, body = call(base, "POST", "/reports", seed)
    print("seed report:", status, body)

    status, full = call(base, "GET", "/chronology/1975")
    show("1975 reference (all trees)", full)

    status, excluded = call(base, "GET", "/chronology/1975?exclude_tree=4")
    show("1975 reference excluding tree 4", excluded)

    status, short = call(base, "POST", "/reports", {"cores": [core(3, 1, {1975: 121})]})
    print("\nre-report tree 3 core 1 without 1976:", status, short)

    status, before = call(base, "GET", "/chronology/1976?exclude_tree=2")
    show("1976 excluding tree 2 after re-report", before)

    status, changed = call(
        base, "POST", "/reports", {"cores": [core(3, 1, {1976: 500})]}
    )
    print("\nre-report tree 3 core 1 with a different 1976 value:", status, changed)

    status, after = call(base, "GET", "/chronology/1976?exclude_tree=2")
    show("1976 excluding tree 2 after the changed re-report", after)

    status, low = call(base, "GET", "/chronology/1976?exclude_tree=5")
    status, low2 = call(base, "GET", "/chronology/1976?exclude_tree=2")
    show("1976 excluding tree 5", low)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
