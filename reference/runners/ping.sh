#!/usr/bin/env bash
# labtech/ping.sh — the pack `ping` contract (Lab Tech v1, mx-7co.2).
#
#   bash labtech/ping.sh <run_id>
#
# Reads runs/<run_id>.pid (written by dispatch.sh), reaches the process, and
# `kill -0`s it. Exit code IS the liveness answer Lab Tech infers progress from:
#
#   exit 0 = ALIVE    (process still running)
#   exit 1 = EXITED   (process gone, or no/again pidfile -> treat as not-running)
#
# PIDFILE LIFECYCLE (Q7). dispatch creates runs/<run_id>.pid; ping REMOVES it the
# first time it observes the process has exited. So a healthy run shows: dispatch
# -> pidfile present, ping=0 (alive) while running, then ping=1 (exited) once and
# the pidfile is gone. dispatch/ping are a matched pair sharing this convention.
#
# NETWORKING (Q7). The pidfile records `<box> <pid>`. v1 box == "local", so ping
# is a local `kill -0` — no ssh, no reconnect (the bead's v1 scope note). The
# remote case is the documented seam at the `kill -0` below: a v2 box label makes
# ping `ssh "$box" kill -0 "$pid"` instead. networking would live HERE (in the
# ping/dispatch pair), still never in the pack.
#
# CAVEAT (v1, acceptable): liveness is pid-only. A pid reused by an unrelated
# process between exit and the next ping could read as alive. The 8-hourly tick
# cadence and short v1 runs make this negligible; v2 can pin pid+start-time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABTECH_HOME="${LABTECH_HOME:-${SCRIPT_DIR}}"
RUNS_DIR="${LABTECH_HOME}/runs"

if [ "$#" -ne 1 ]; then
  echo "Usage: bash labtech/ping.sh <run_id>" >&2
  exit 2
fi

run_id="$1"
pidfile="${RUNS_DIR}/${run_id}.pid"

if [ ! -f "$pidfile" ]; then
  # No pidfile: the run never launched here, or already exited and was reaped.
  # Either way it is not running -> EXITED.
  echo "[ping] run_id=$run_id: no pidfile ($pidfile) -> exited" >&2
  exit 1
fi

# Pidfile format: one line, `<box> <pid>`.
read -r box pid _ <"$pidfile" || true
if [ -z "${pid:-}" ]; then
  echo "[ping] run_id=$run_id: malformed pidfile ($pidfile) -> exited" >&2
  rm -f "$pidfile"
  exit 1
fi

# --- liveness probe (networking seam, Q7) ---
# v1: box == local -> local kill -0.
# v2 swaps ONLY this to: ssh "$box" kill -0 "$pid"   (box = a hostname/tailscale name)
if [ "$box" = "local" ]; then
  alive() { kill -0 "$pid" 2>/dev/null; }
else
  echo "[ping] run_id=$run_id: box='$box' is remote; v2 ssh path not implemented" >&2
  exit 2
fi

if alive; then
  echo "[ping] run_id=$run_id box=$box pid=$pid -> alive" >&2
  exit 0
fi

# Exited: clean up the stale pidfile so the next tick sees a settled state.
rm -f "$pidfile"
echo "[ping] run_id=$run_id box=$box pid=$pid -> exited (pidfile removed)" >&2
exit 1
