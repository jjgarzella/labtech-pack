#!/usr/bin/env bash
# labtech/dispatch.sh — the pack `dispatch` contract.
#
# Lab Tech mints a run_id (the experiment bead id), resolves the catalog
# `command` template into a concrete command line, then calls:
#
#   bash labtech/dispatch.sh <run_id> <resolved command …>
#
# dispatch launches that ALREADY-RESOLVED command detached via `nohup`, records
# a pidfile keyed by run_id, and exits. Exit 0 = launched. It does NOT wait for
# the computation and it does NO networking — per design Q7, networking lives
# inside the resolved command (the rig's run_wrapper.sh), never in the
# pack/runner. dispatch only needs the pid to be trackable, which run_wrapper.sh
# guarantees by `exec`-ing all the way down to the Julia process.
#
# PIDFILE (Q7). Written to runs/<run_id>.pid — the pack-owned path convention.
# One whitespace-separated line: `<box> <pid>`.
#   - box: where the process runs. v1 == "local" (compute box == dispatch box).
#          v2 puts a hostname/tailscale name here so ping can reconnect.
#   - pid: the pid to `kill -0`. v1 == the local nohup pid; because run_wrapper
#          execs through to julia, this single pid tracks the live computation.
# The pidfile is REMOVED by ping when it first observes the process has exited
# (see ping.sh) — dispatch only creates it.
#
# LAYOUT. runs/ and the per-run logfile live under $LABTECH_HOME (default: the
# directory holding these scripts), so "runs/<run_id>.pid" is honored relative to
# a single, overridable base. Both are runtime state and are gitignored.
#
# Usage:
#   bash labtech/dispatch.sh <run_id> <command> [args…]
#   bash labtech/dispatch.sh <run_id> "<command> [args…]"   # single-string form
# Example:
#   bash labtech/dispatch.sh example-abc \
#     bash /abs/labtech/run_wrapper.sh <arg…> example-abc
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABTECH_HOME="${LABTECH_HOME:-${SCRIPT_DIR}}"
RUNS_DIR="${LABTECH_HOME}/runs"

# v1: compute box == dispatch box. The pidfile carries this so ping knows whether
# to `kill -0` locally or (v2) reconnect over ssh first.
LABTECH_BOX="${LABTECH_BOX:-local}"

if [ "$#" -lt 2 ]; then
  echo "Usage: bash labtech/dispatch.sh <run_id> <command> [args…]" >&2
  exit 2
fi

run_id="$1"; shift
case "$run_id" in
  */*|"") echo "FAIL: bad run_id '$run_id' (must be a non-empty, slash-free label)" >&2; exit 2 ;;
esac

pidfile="${RUNS_DIR}/${run_id}.pid"
logfile="${RUNS_DIR}/${run_id}.log"
mkdir -p "$RUNS_DIR"

# Two accepted call shapes, both yielding a recorded pid that exec-chains to the
# computation (so ping's `kill -0` tracks the real run, not a transient shell):
#   - many args  -> the command is already split into argv; run it directly.
#                   run_wrapper.sh execs onward, so $! becomes julia.
#   - one arg    -> a single resolved-command STRING (the Q7 `dispatch RUN_ID
#                   RESOLVED_COMMAND` shape, e.g. from the pack). `exec` inside
#                   `bash -c` replaces the shell with the command so the recorded
#                   pid is the command's, regardless of bash's exec optimization.
if [ "$#" -eq 1 ]; then
  set -- bash -c "exec $1"
fi

# nohup + </dev/null detaches from SIGHUP and the controlling terminal; all
# output is captured to the per-run logfile (never into agent context — Lab Tech
# reads results via harvest, not these logs).
nohup "$@" >"$logfile" 2>&1 </dev/null &
pid=$!

printf '%s %s\n' "$LABTECH_BOX" "$pid" >"$pidfile"

echo "[dispatch] run_id=$run_id box=$LABTECH_BOX pid=$pid -> $pidfile (log: $logfile)" >&2
exit 0
