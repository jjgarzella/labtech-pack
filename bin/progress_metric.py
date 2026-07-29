#!/usr/bin/env python3
"""progress_metric.py — count distinct result-keys in a harvest snapshot.

Lab Tech is never told "done"; it infers progress by diffing harvests across cycles
(Q1/Q2). A typical progress metric reads "count of distinct (colA, colB) pairs among
the rows matching this filter" — but the *filter* and *uniqueness key* are goal-owned
metadata, not pack constants:

    goal metadata.filter         -> {"<col>": <value>}
    goal metadata.uniqueness_key -> ["<colA>", "<colB>"]

so this tool stays generic: given a harvest JSON file (an array of row objects, exactly
what `harvest.sh` dumps from the results table), a filter object, and a uniqueness-key
list, it reports the number of DISTINCT key-tuples among the rows that match the filter.

The tick formula calls it three times per cycle — on the current snapshot, the previous
cycle's snapshot, and the baseline (first-ever) snapshot — and reports both deltas. The
baseline subtraction is what makes an already-populated results table safe: pre-existing
rows are counted in the baseline, so only genuinely new distinct keys show up as
this-run progress.

Filter matching is by STRING equality (str(row[k]) == str(filter[k])) so an int in the
JSON matches a filter value of that int or its string form alike. A row missing a
filter key, or a filter/key referencing an absent column, simply does not match / is
skipped — never a crash, so a transient schema hiccup degrades to "0 new" rather than
aborting the tick.

Modes
-----
  (default)     print the distinct-key count as a bare integer
  --json        print {"total_rows", "matched_rows", "distinct_count"}
  --dump-keys   print each distinct key-tuple as a compact JSON array, one per line,
                sorted — feed two of these to `comm`/`sort` to find which keys are NEW

Usage
-----
  progress_metric.py --harvest harvest/<cycle>.json \
      --filter '{"<col>":<value>}' --key '["<colA>","<colB>"]' [--json|--dump-keys]
"""

from __future__ import annotations

import argparse
import json
import sys


def die(msg: str) -> None:
    print(f"progress_metric: {msg}", file=sys.stderr)
    sys.exit(2)


def load_rows(path: str) -> list:
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        die(f"harvest file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"harvest file {path} is not valid JSON: {e}")
    if isinstance(data, dict):
        # A PostgREST error body is an object, not an array — treat as no rows but be loud.
        die(f"harvest file {path} is a JSON object, not a row array "
            f"(PostgREST error?): {json.dumps(data)[:200]}")
    if not isinstance(data, list):
        die(f"harvest file {path} did not contain a JSON array of rows")
    return data


def matches(row: dict, flt: dict) -> bool:
    if not isinstance(row, dict):
        return False
    for k, v in flt.items():
        if k not in row:
            return False
        if str(row[k]) != str(v):
            return False
    return True


def key_tuple(row: dict, key: list):
    # Missing column -> None component; a row missing a key column still gets counted as a
    # distinct tuple containing None, which is rare and visible rather than silently dropped.
    return tuple(row.get(k) for k in key)


def main() -> None:
    ap = argparse.ArgumentParser(description="Count distinct result-keys in a harvest.")
    ap.add_argument("--harvest", required=True)
    ap.add_argument("--filter", default="{}", help='JSON object, e.g. {"<col>":<value>}')
    ap.add_argument("--key", required=True, help='JSON array of column names, e.g. ["colA","colB"]')
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--dump-keys", action="store_true")
    args = ap.parse_args()

    try:
        flt = json.loads(args.filter)
        key = json.loads(args.key)
    except json.JSONDecodeError as e:
        die(f"--filter/--key must be JSON: {e}")
    if not isinstance(flt, dict):
        die("--filter must be a JSON object")
    if not isinstance(key, list) or not key:
        die("--key must be a non-empty JSON array")

    rows = load_rows(args.harvest)
    matched = [r for r in rows if matches(r, flt)]
    distinct = sorted({key_tuple(r, key) for r in matched}, key=lambda t: json.dumps(t, default=str))

    if args.dump_keys:
        for t in distinct:
            print(json.dumps(list(t), default=str))
    elif args.json:
        print(json.dumps({
            "total_rows": len(rows),
            "matched_rows": len(matched),
            "distinct_count": len(distinct),
        }))
    else:
        print(len(distinct))


if __name__ == "__main__":
    main()
