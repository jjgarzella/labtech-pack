# labtech — autonomous experiment-runner pack

Lab Tech is a Gas City **pack** that autonomously dispatches math experiments,
tracks them across cycles, and synthesizes results — **without ever being told
"done"**. It infers progress by diffing a results "DB" each cycle and watching a
goal's progress metric climb, then plateau. It is project-agnostic: it serves
whichever rig imports it.

The design is twelve resolved decisions (Q1–Q12); the relevant ones are cited
inline below and in each file. Where this pack came from, and what was actually
measured on the rig it was built against, is in [HISTORY.md](HISTORY.md) — those
are observations, not defaults.

## Mental model in one paragraph

The `labtech-tick` **order** fires on a cadence and pours an ephemeral
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
├── HISTORY.md                 provenance + the observations behind the caveats
├── formulas/
│   └── labtech-tick.toml      the one-tick cycle (orient→reap→harvest-diff→
│                              labbook→dispatch→finish). The knobs (concurrency
│                              cap, paths, lab_book id, seed_N) are its [vars];
│                              labtech_home / labtech_pack / lab_book are REQUIRED.
├── orders/
│   └── labtech-tick.toml      the trigger: action=labtech-tick formula,
│                              cooldown 8h, routed rig-relative to labtech.tick.
│                              Override cadence/target rig-locally.
├── agents/
│   └── tick/                  the dedicated single-instance tick worker.
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

**The rig owns the catalog + runner scripts + runtime state**, rig-local — keep
them in a directory of the rig's choosing (e.g. `labtech/`) and point the
formula's `labtech_home` var at it:

| rig-local file | role |
|----------------|------|
| `experiments.toml` | the **menu** of runnable experiments (Q4/Q5 OPTION A) — NOT a run log. Each `[[experiment]]`: `command` template, `[experiment.constants]`, tunable `[[parameter]]` blocks, `table`. |
| `dispatch.sh` `ping.sh` `harvest.sh` | the exec **contract**. **Networking lives here**, never in the pack. |
| `run_wrapper.sh` `run_experiment.{sh,jl}` | the experiment **engine + networking wrapper** — the catalog `command` points at them. |
| `runs/` `harvest/` `state/` | runtime state (gitignored). |

The contract scripts under `reference/runners/` are verbatim copies so a new rig
has a working starting point and the contract is documented in code. They are
**not** what runs.

## The data model (Q3/Q6)

- **lab-book** (formula var `lab_book`) — Lab-Tech-owned root, **one per rig**,
  outlives every goal-group. Experiment beads attach as children. Holds the
  per-cycle harvest digests + cross-cycle synthesis.
- **experiment** bead — one per run; **`run_id == bead id`** (Q7). Parent =
  lab-book; links to its goal via `goal_id`/`goal_group_id` metadata. **Lifecycle
  = PINNED, not closed**: running = `open`; exited = `pinned=1`. The reap step
  sets that itself — it prefers a native pin flag where the `bd` build offers one,
  and otherwise writes `pinned=1` via `gc bd sql`. In-flight = open, non-pinned
  experiment children of the lab-book.
- **goal-group / goal** — crew-authored, **READ-ONLY to Lab Tech**. The goal's
  metadata names the `table`, `filter`, and `uniqueness_key` that define
  progress; the pack hard-codes no progress rule. A goal that names no
  `uniqueness_key` gets harvested but not scored.

## Baseline caveat

A rig's results table is normally **already non-empty** when Lab Tech starts on
it. The first tick's harvest is the **baseline**; only distinct keys appearing
after it count as progress. The diff subtracts the baseline so pre-existing rows
never read as new results — on the first tick both deltas are 0 by construction.

## Box caveat

Compute cost grows much faster in `p` than in `N`, and a thread-greedy run can
**OOM-kill the compute box silently** — nothing reports the kill. Where that
ceiling sits is a property of the rig's box and experiment, so record your own
measured limits in the rig's Lab Tech docs; this pack names no threshold. Two
rules hold anywhere: keep launches thread-capped (e.g. `taskset -c 0-2`) and
prefer raising `N` over `p`.

## Wiring a rig

1. give this pack a git remote and add it to the city (a submodule under
   `packs/labtech` works well; commit `.gitmodules`, init/update on clones);
2. import it into the rig in `city.toml`, and supply the three REQUIRED formula
   vars — the pack ships no defaults for them on purpose:

   ```toml
   [[rigs]]
   name = "<rig>"
   [rigs.imports.labtech]
   source = "packs/labtech"
   [rigs.formula_vars]
   labtech_home = "/abs/path/to/<rig>/labtech"   # runner scripts + experiments.toml
   labtech_pack = "/abs/path/to/<city>/packs/labtech"
   lab_book     = "<lab-book-bead-id>"           # create this bead first
   ```

3. copy `reference/runners/` into `<labtech_home>` and adapt the networking bits
   to your results DB and compute box;
4. tune `orders/labtech-tick.toml`'s cadence rig-locally if 8h does not suit;
5. confirm a real tick fires end-to-end.

## Running a tick

```bash
gc order run labtech-tick        # one full cycle end-to-end, then exit
```

This needs the import above plus live infra (the results DB and a compute box
reachable from the runner scripts). Before that lands, validate the pieces
directly:

```bash
# formula compiles + the helpers resolve against the live catalog
gc formula show labtech-tick
python3 bin/resolve_command.py render --catalog <labtech_home>/experiments.toml \
    --experiment <experiment-name> --run-id example-abc -p p=<prime> -p N=<count>
python3 bin/progress_metric.py --harvest <snapshot>.json \
    --filter '{"<col>":<value>}' --key '["<col1>","<col2>"]'
```

## Acceptance gate

> `gc order run` (manual) executes one full cycle end-to-end: dispatches under
> the cap, records the experiment bead, harvests, writes a lab-book digest, pins
> on exit. Respects the catalog's N default.
