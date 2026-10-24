# Provenance and validation history

Everything on this page is **history, not contract**. It records where Lab Tech
came from and what was actually observed on the one rig it was built against, so
that the reusable files (`pack.toml`, `formulas/`, `agents/`, `orders/`, `bin/`,
`README.md`) can stay free of another rig's filesystem, bead ids, tables, and
box limits.

**Do not read any number here as a default.** The observations below are
properties of a specific box, a specific results schema, and a specific `bd`
build at a specific time. Your rig's are different.

## v1 build

Lab Tech v1 was built in the `math-experiments` rig of the `math-city` city,
under build epic `mx-7co`, against a Newton-polygon experiment over quintic
surface deformations. The design rests on twelve resolved decisions (Q1–Q12);
those Q-numbers are still cited inline throughout the pack because they name
*design rules*, which do generalize. The `mx-*` bead ids that used to accompany
them are collected here instead.

| Step | What it settled |
|------|-----------------|
| `mx-7co.1` / `mx-7co.8` | the experiment engine + networking wrapper (`run_wrapper.sh`, `run_experiment.{sh,jl}`) — rig-owned |
| `mx-7co.2` | the three-script exec contract (`dispatch.sh` / `ping.sh` / `harvest.sh`); the box caveat below |
| `mx-7co.3` | the catalog (`experiments.toml`) as a menu, not a run log |
| `mx-7co.4` | the lab-book as a Lab-Tech-owned root, one per rig |
| `mx-7co.5` | the acceptance gate: one manual `gc order run` executes a full cycle end-to-end; the baseline caveat; PINNED-not-closed lifecycle |
| `mx-7co.6` | wiring: private remote + submodule + `[rigs.imports.labtech]`, and the flip from manual to a scheduled trigger |
| `mx-7co.10` | N=1 per tick as the catalog default — fast cycles beat big batches |
| `mx-7co.13` | go-live on an autonomous cadence |
| `mx-7co.16` | harvest-snapshot naming must be filtered to the timestamp pattern (below) |
| `mx-7co.17` | the dedicated single-instance tick agent, replacing the shared polecat pool |
| `mx-7co.18` | single-session tick instead of a multi-step graph.v2 tick (below) |

The v1 lab-book was `mx-p03`; the v1 goal was `mx-9w6.1` — distinct Newton
polygons at p=17 in the results table `n4_d5_np`, with
`uniqueness_key = ["slopes", "slopelengths"]` and `filter = {"p": 17}`.

## Cadence

The cadence has moved more than once. The epic's original default was 8-hourly
(Q10). An overseer decision (`mc-wisp-y8tzl3`) later cut it to every 30 minutes
on the grounds that the box sustained fast N=1 cycles. The pack now ships the
conservative 8h cooldown and expects each rig to tune it locally — see
`orders/labtech-tick.toml`.

## Box observation — why the pack refuses to name a p ceiling

On the `math-experiments` compute box (~7.7 GB as provisioned), as of 2026-06,
thread-greedy Julia runs (`-t auto`) were observed to **OOM-kill silently near
p=23**. p=17 ran fine at modest N. Nothing announced the kill; the run simply
stopped producing rows.

The *durable* lesson is in the pack (cap threads, prefer raising N over p, ask
the rig's own docs for its measured limits). The *threshold* stayed here,
because p≈23 is a fact about 7.7 GB of RAM and this experiment's memory profile
— not about Lab Tech.

## Runtime observations

- **Multi-step graph.v2 ticks stalled after step one** (`mx-7co.18`, 2026-06):
  the control-dispatcher that hands off between steps proved flaky in that
  runtime. This was a rig/runtime observation, never a property of graph.v2. The
  single-session tick was kept anyway on its own merits — a tick has no
  cross-step state to hand off, so there is nothing for a handoff to drop.
- **`GC_BEAD_ID` was not injected** into poured wisps (`mx-7co.14/.15`), so the
  formula claims its routed work bead itself. It still honors `GC_BEAD_ID` when
  present, which makes the behavior correct either way, not just here.
- **Pool over-spawn** (`mx-7co.14/.15`): routing ticks at the shared
  `gasvillage.polecat` pool (max 5) over-poured — surplus polecats raced the
  formula's steps and several booted with an empty `GC_BEAD_ID` and aborted.
  `mx-7co.17` replaced the pool with this pack's own `max_active_sessions = 1`
  agent.
- **No `--pinned` flag** on the `bd` build in use at v1, hence the raw-SQL pin
  (`UPDATE issues SET pinned=1 ...`). The formula now tries the flag first and
  falls back to SQL.
- **Stray harvest files poison the baseline** (`mx-7co.16`): a leftover
  `smoke-*.json` in `harvest/` sorts after digits and was mis-picked as the
  newest snapshot. The formula now matches only the
  `<YYYYMMDD>T<HHMMSS>Z.json` cycle-name pattern.

## Reference runner scripts

`reference/runners/{dispatch,ping,harvest}.sh` were captured verbatim from the
`math-experiments` rig's as-built scripts (`mx-7co.2`). They are a starting
point, not what runs — see `reference/README.md`.


## 2026-10-24: persistent George

At the overseer's request, Lab Tech now supplies an always-on Codex
`gpt-6.1-sol` agent named George, in honor of George E. P. Box. The prompt
explicitly separates the software agent from its namesake. The old tick worker
and cycle formula are retained for explicit compatibility use; their automatic
order was moved to `reference/orders/` so enabling George does not dispatch
historical experiments. Existing lab books, experiment records and artifacts
are retained. Current explicit scope and holds govern all new work.
