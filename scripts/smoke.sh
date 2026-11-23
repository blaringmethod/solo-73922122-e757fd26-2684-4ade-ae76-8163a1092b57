#!/usr/bin/env bash
# End-to-end smoke test against a running container: seeds a stand, checks a
# normal query, an exclusion query, a whole-core re-report that drops a year,
# and a rollback-style validation rejection. Requires curl (+ python3 for JSON).
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "== health =="
curl -fsS "$BASE/health"; echo

echo "== seed =="
curl -fsS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "cores": [
    {"tree_no":1,"core_no":1,"years":{"1975":90,"1976":92}},
    {"tree_no":1,"core_no":2,"years":{"1975":96,"1976":94}},
    {"tree_no":2,"core_no":1,"years":{"1975":104,"1976":100}},
    {"tree_no":2,"core_no":2,"years":{"1975":110,"1976":102}},
    {"tree_no":3,"core_no":1,"years":{"1975":121,"1976":108}},
    {"tree_no":3,"core_no":2,"years":{"1975":127,"1976":110}},
    {"tree_no":4,"core_no":1,"years":{"1975":140,"1976":118}},
    {"tree_no":5,"core_no":1,"years":{"1975":160,"1976":126}}
  ]
}' | json "d"; echo

echo "== 1975 all =="
curl -fsS "$BASE/chronology/1975" | json "d['sample_trees'], d['value'], d['read_cost']"

echo "== 1975 excluding tree 4 =="
curl -fsS "$BASE/chronology/1975?exclude_tree=4" | json "d['sample_trees'], d['value'], d['excluded_tree_present']"

echo "== re-report tree 3 core 1 without 1976 (whole-core replacement) =="
curl -fsS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "cores": [{"tree_no":3,"core_no":1,"years":{"1975":121}}]
}' | json "d"; echo

echo "== 1976 excluding tree 2 (tree 3 keeps only core 2) =="
curl -fsS "$BASE/chronology/1976?exclude_tree=2" | json "d['sample_trees'], d['value']"

echo "== invalid report must be rejected and change nothing =="
code=$(curl -s -o /tmp/smoke_reject.json -w '%{http_code}' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' \
  -d '{"cores":[{"tree_no":1,"core_no":1,"years":{"1976":-5}}]}')
echo "status=$code"

echo "== drop 1976 from trees 1 and 2 (whole-core replacement) =="
curl -fsS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "cores": [
    {"tree_no":1,"core_no":1,"years":{"1975":90}},
    {"tree_no":1,"core_no":2,"years":{"1975":96}},
    {"tree_no":2,"core_no":1,"years":{"1975":104}},
    {"tree_no":2,"core_no":2,"years":{"1975":110}}
  ]
}' | json "d['data_version'], d['years_changed']"; echo

echo "== 3 trees left, excluding tree 3 leaves 2 -> insufficient =="
curl -fsS "$BASE/chronology/1976" | json "d['sample_trees'], d['status'], d['value']"
curl -fsS "$BASE/chronology/1976?exclude_tree=3" | json "d['sample_trees'], d['status'], d['value']"
