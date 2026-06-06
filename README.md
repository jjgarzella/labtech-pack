# labtech — autonomous experiment-runner pack

Lab Tech is a Gas City **pack** that autonomously dispatches math experiments,
tracks them across cycles, and synthesizes results — **without ever being told
"done"**. It infers progress by diffing a results "DB" each cycle and watching a
goal's progress metric climb, then plateau. It is project-agnostic and rides the
**math-experiments** rig (it can serve any rig that imports it).

This is the **v1** build (epic `mx-7co`). The design is twelve resolved
decisions (Q1–Q12); the relevant ones are cited inline below and in each file.

## Mental model in one paragraph

A cron (v1: manual) fires the `labtech-tick` **order**, which pours an ephemeral
wisp that runs the `labtech-tick` **formula** for exactly **one tick** and exits.
A tick: pick the goal to serve → ping in-flight runs and **pin** the ones that
exited → **harvest** the results table to disk → **diff** it vs the previous
cycle and the baseline → append a digest to the **lab book** → **dispatch** at
most one new run under the concurrency cap → close the tick bead. All durable
state lives in beads and harvest files, so a dropped tick is safe — the next
fire re-derives everything and starts clean.

## Layout

```
packs/labtech/
├── pack.toml                  pack manifest (schema 2)
├── README.md                  you are here
├── formulas/
│   └── labtech-tick.toml      the one-tick cycle, graph.v2 (orient→reap→
│                              harvest-diff→labbook→dispatch→finish). The knobs
│                              (concurrency cap, paths, lab_book id, seed_N) are
│                              its [vars].
├── orders/
│   └── labtech-tick.toml      the trigger: action=labtech-tick formula.
│                              v1 trigger=manual (the acceptance gate);
│                              step 9 flips it to cron 0 */8 * * *.
├── agents/
│   └── tick/                  the dedicated single-instance tick worker (mx-7co.17).
│       ├── agent.toml         max_active_sessions=1 → one instance per tick, no
│       │                      pool over-spawn; the order routes here.
│       └── prompt.template.md Lab Tech's "brains" / the agent's standing prompt
│                              (data model, read-only goal rule, baseline caveat,
│                              the N-ramp + synthesis judgment).
├── bin/
│   ├── resolve_command.py     render a catalog `command` template + run_id +
│                              chosen params → the exact line `dispatch` accepts.
│   └── progress_metric.py     count distinct result-keys (the goal's
│                              uniqueness_key under its filter) in a harvest.
└── reference/
    ├── README.md              what these reference templates are / aren't
    └── runners/               verbatim copies of the rig-local exec contract:
        ├── dispatch.sh        launch detached via nohup, write runs/<id>.pid
        ├── ping.sh            kill -0 the pid; exit 0=alive / 1=exited
        └── harvest.sh         dump the results table → harvest/<cycle>.json
```

## The pack/rig split (Q7/Q9)

**The pack owns logic + templates and is networking-free** — it only renders
text (`bin/`), encodes the cycle (`formulas/`, `agents/`), and shells out to the
rig's contract scripts.

**The rig owns the catalog + runner scripts + runtime state**, rig-local (the
math-experiments rig keeps them in `labtech/`, pointed at by the formula's
`labtech_home` var):

| rig-local file | role |
|----------------|------|
| `experiments.toml` | the **menu** of runnable experiments (Q4/Q5 OPTION A) — NOT a run log. Each `[[experiment]]`: `command` template, `[experiment.constants]`, tunable `[[parameter]]` blocks, `table`. (mx-7co.3) |
| `dispatch.sh` `ping.sh` `harvest.sh` | the exec **contract** (mx-7co.2). **Networking lives here**, never in the pack. |
| `run_wrapper.sh` `run_experiment.{sh,jl}` | the experiment **engine + networking wrapper** (mx-7co.1/8) — the catalog `command` points at them. |
| `runs/` `harvest/` `state/` | runtime state (gitignored). |

The contract scripts under `reference/runners/` are verbatim copies so a new rig
has a working starting point and the contract is documented in code. They are
**not** what runs.

## The data model (Q3/Q6)

- **lab-book** (`mx-p03`) — Lab-Tech-owned root, **one per rig**, outlives every
  goal-group. Experiment beads attach as children. Holds the per-cycle harvest
  digests + cross-cycle synthesis.
- **experiment** bead — one per run; **`run_id == bead id`** (Q7). Parent =
  lab-book; links to its goal via `goal_id`/`goal_group_id` metadata. **Lifecycle
  = PINNED, not closed**: running = `open`; exited = `pinned=1`
  (`gc bd sql "UPDATE issues SET pinned=1 WHERE id='<exp>'"` — this bd build has
  **no `--pinned` flag**, mx-7co.5). In-flight = open, non-pinned experiment
  children of the lab-book.
- **goal-group / goal** — crew-authored, **READ-ONLY to Lab Tech**. The goal's
  metadata names the `table`, `filter`, and `uniqueness_key` that define
  progress; the pack hard-codes no progress rule. v1 goal: `mx-9w6.1` — distinct
  Newton polygons at p=17 in `n4_d5_np`.

## Baseline caveat (mx-7co.5)

`n4_d5_np` is **non-empty at v1 start**. The first tick's harvest is the
**baseline**; only distinct keys appearing after it count as progress. The diff
subtracts the baseline so pre-existing rows never read as new results — on the
first tick both deltas are 0 by construction.

## Box caveat (mx-7co.2)

The compute box is ~7.7 GB. Thread-greedy Julia (`-t auto`) **OOM-kills silently
around p=23**. Keep launches thread-capped (e.g. `taskset -c 0-2`) and prefer
raising `N` over `p`. The v1 goal fixes p=17, which is safe at modest `N`.

## Running a tick

v1 is **manual** (the mx-7co.5 acceptance gate):

```bash
gc order run labtech-tick        # one full cycle end-to-end, then exit
```

This requires the pack to be composed into the rig (step 9's
`[rigs.imports.labtech]`) and live infra (Supabase results DB + the CPU box
reachable from the runner scripts). Until that import lands, validate the pieces
directly:

```bash
# formula compiles + the helpers resolve against the live catalog
gc formula show labtech-tick
python3 bin/resolve_command.py render --catalog <rig>/labtech/experiments.toml \
    --experiment fermat-deformations-quintic --run-id mx-TEST -p p=17 -p N=200
python3 bin/progress_metric.py --harvest <some>.json \
    --filter '{"p":17}' --key '["slopes","slopelengths"]'
```

## Acceptance gate (mx-7co.5)

> `gc order run` (manual) executes one full cycle end-to-end: dispatches under
> the cap, records the experiment bead, harvests, writes a lab-book digest, pins
> on exit. Respects N=1.

## Wiring seam → step 9 (mx-7co.6)

This pack is self-contained but **not yet wired**. Step 9 owns:

1. give `packs/labtech` a **private GH remote** and add it to math-city as a git
   **submodule** at `packs/labtech` (commit `.gitmodules`; init/update on clones);
2. add `[rigs.imports.labtech] source = "packs/labtech"` to the math-experiments
   rig so the rig sees this formula + order;
3. flip `orders/labtech-tick.toml` to `trigger = "cron"`, `schedule =
   "0 */8 * * *"` (the pack-level adjustable cadence, Q10) and set its
   `pool`/target to the rig's tick worker;
4. confirm a real scheduled tick fires end-to-end on cron.
