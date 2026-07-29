# Reference runner/harvest templates

Lab Tech's exec contract (Q1/Q7) is **three operator-provided scripts** that the
pack shells out to but never owns — networking lives in them, not in the pack:

| script | contract | exit / output |
|--------|----------|---------------|
| `dispatch.sh <run_id> <resolved-command>` | launch the already-resolved command detached via `nohup`, write `runs/<run_id>.pid` (`<box> <pid>`), exit | exit 0 = launched |
| `ping.sh <run_id>` | read the pidfile, `kill -0` the process; remove a stale pidfile on first observed exit | exit 0 = alive, 1 = exited |
| `harvest.sh [cycle] [table]` | dump the results table to stdout, or to `harvest/<cycle>.json` | nonzero on fetch failure |

The copies under `runners/` are **reference templates** (Q9: *the pack ships
reference runner/harvest templates; the rig owns the live copies*). They were
captured verbatim from a real rig's as-built scripts (see ../HISTORY.md) so:

- a **new rig** adopting Lab Tech has a working starting point — copy them into
  the rig's runner dir (e.g. `labtech/` or `.labtech/`), then adapt the
  networking/secret bits (`harvest.sh`'s results-DB URL + key, `ping.sh`'s remote
  `kill -0` seam) to that rig's results DB and compute box; and
- the contract the formula depends on is **documented in code**, not just prose.

These reference copies are NOT what runs. The live scripts live rig-local
(pointed at by the formula's `labtech_home` var); the rig may diverge from these
templates for rig-specific transport. Keep the contract — the `runs/<run_id>.pid`
path convention and the exit-code meanings — stable, since the pack's
`dispatch`/`ping`/`harvest` calls assume it.

The experiment **engine + networking wrapper** (`run_wrapper.sh`,
`run_experiment.{sh,jl}`) are experiment-specific and rig-owned; the catalog
`command` points at them. They are not shipped here
because they vary per experiment; only the dispatch/ping/harvest contract is
generic enough to template.
