#!/usr/bin/env bash
# labtech/harvest.sh — the pack `harvest` contract (Lab Tech v1, mx-7co.2).
#
# Dumps the current results-DB state so Lab Tech can infer progress by diffing
# harvests across cycles (it is never told "done"). v1 results DB == Supabase;
# harvest == fetch_table(<table>) over the Supabase REST API, the SAME transport
# run_experiment.jl writes through (curl; the DeRham project env lacks HTTP.jl).
#
# Per design Q7 the canonical shape is "no args -> dump to stdout; the pack
# captures it to harvest/<cycle>.json (never into agent context)". Two modes:
#
#   bash labtech/harvest.sh                 # dump JSON of the table to STDOUT
#                                           #   (pack form: harvest > harvest/<cycle>.json)
#   bash labtech/harvest.sh <cycle>         # write JSON to harvest/<cycle>.json
#                                           #   (rig-local form; honors the path convention)
#
# Both produce the identical full snapshot; pick whichever the caller prefers.
# Table defaults to the v1 quintic-surface target n4_d5_np; override with
# LABTECH_TABLE or a 2nd positional arg (the table is judgment-based per Q7, named
# in the goal/goal-group body).
#
# NETWORKING (Q7) lives HERE: harvest sources the Supabase key and talks to the
# REST API itself. The pack stays networking-free.
#
# Usage:
#   bash labtech/harvest.sh [cycle] [table]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABTECH_HOME="${LABTECH_HOME:-${SCRIPT_DIR}}"
HARVEST_DIR="${LABTECH_HOME}/harvest"
ENV_FILE="${LABTECH_ENV_FILE:-${HOME}/.config/labtech/env}"

cycle="${1:-}"
table="${2:-${LABTECH_TABLE:-n4_d5_np}}"

# --- secret loading (Q7), same idempotent pattern as run_wrapper.sh ---
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
if [ -z "${SUPABASE_ANON_KEY:-}" ]; then
  echo "FAIL: SUPABASE_ANON_KEY unset. Expected it in $ENV_FILE" >&2
  exit 1
fi
SUPABASE_URL="${SUPABASE_URL:-https://oigzppmsalxtinyuegpy.supabase.co}"

# fetch_table: GET <url>/rest/v1/<table>?select=*  (mirrors run_experiment.jl).
# -sS quiet-but-report-errors; --fail-with-body exits nonzero on HTTP >=400 yet
# still prints the body so a PostgREST error (e.g. missing table) is visible.
fetch_table() {
  curl -sS --fail-with-body \
    -H "apikey: ${SUPABASE_ANON_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
    -H "Content-Type: application/json" \
    "${SUPABASE_URL}/rest/v1/${table}?select=*"
}

if [ -n "$cycle" ]; then
  case "$cycle" in */*) echo "FAIL: bad cycle '$cycle' (slash-free label)" >&2; exit 2 ;; esac
  mkdir -p "$HARVEST_DIR"
  out="${HARVEST_DIR}/${cycle}.json"
  # Stage to a temp file so a mid-fetch failure never leaves a truncated snapshot.
  tmp="${out}.tmp.$$"
  if ! fetch_table >"$tmp"; then
    rm -f "$tmp"
    echo "FAIL: harvest of table '$table' failed (see error above)" >&2
    exit 1
  fi
  mv "$tmp" "$out"
  echo "[harvest] table=$table cycle=$cycle -> $out" >&2
else
  fetch_table
fi
