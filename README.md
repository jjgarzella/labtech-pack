# Lab Tech — George

George is an **always-on, persistent lab technician** for each rig importing this
pack. His name honors **George E. P. Box**; he is a software agent, not Box and
not an impersonation. The default provider is **Codex**, model **gpt-6.1-sol**,
with high reasoning effort. The pack declares one named session per rig and
does not also warm an anonymous pool.

George keeps the lab book, monitors authorized experiments, compares harvests
with their baseline, and discusses results and next steps with the overseer.
He stays available between cycles. Startup is not permission to resume an old
experiment or launch a new one; explicit assignments and holds remain in force.

## Import and address George

```toml
[[rigs]]
name = "<rig>"
[rigs.imports.labtech]
source = "packs/labtech"
[rigs.formula_vars]
labtech_home = "/abs/path/to/<rig>/labtech"
labtech_pack = "/abs/path/to/<city>/packs/labtech"
lab_book = "<existing-lab-book-bead>"
```

With the `labtech` binding, his configured session and mail address are
`<rig>/labtech.george`. Use `gc session attach <rig>/labtech.george` to talk to
him. Importing rigs may override provider/model settings through normal agent
patches. The required paths and lab-book ID are rig-specific; the pack invents
none of them.

## Responsibilities and durable state

- Preserve the current assignment, scientific parameters, source/runtime pins,
  experiment ownership, and explicit pauses.
- Recover active runs from their recorded IDs, pidfiles, and result evidence.
  An exited process does not establish success.
- Keep narrative digests and recommendations in the lab book. Keep raw logs,
  harvest snapshots, and large tables on disk.
- Treat the first harvest as baseline. Measure new distinct keys using the
  goal's own filter and uniqueness key; separate previous-cycle and baseline
  deltas. A plateau informs a recommendation rather than authorizing new scope.
- Respect configured experiment concurrency and evaluator isolation. Historical
  machine-memory scheduling policies do not override the city's current policy.
- Remain available for conversation and routed work without a busy polling loop.

## Pack and rig boundary

The pack owns `agents/george/`, the named-session declaration, catalog rendering
(`bin/resolve_command.py`), progress counting (`bin/progress_metric.py`), and
reference runner contracts. The rig owns `experiments.toml`, `dispatch.sh`,
`ping.sh`, `harvest.sh`, the experiment engine, and `runs/`, `harvest/`, and
`state/` artifacts. Inspect those actual paths before running a migrated catalog.
See [reference runners](reference/README.md) for the execution contract.

## Migration from ephemeral ticks

The old `agents/tick/` and `formulas/labtech-tick.toml` remain available for
explicit compatibility use. They run one cycle and drain their own ephemeral
worker; George must not use that behavior for his persistent session. The
legacy formula also assumes older bead interfaces (including a `goal` issue
type); verify those interfaces before reusing it. It is not the new agent's
default execution path.

The former automatic eight-hour order is preserved at
`reference/orders/labtech-tick.toml`, outside the pack's active `orders/`
directory. It no longer starts historical experiments just because the new
persistent agent is enabled. A rig can deliberately install an order after
its current experiment scope and runner compatibility are established; avoid
running two dispatchers for the same lab book.

Existing lab books, run IDs, experiment beads, files, and source history are
preserved. No old goal is resumed by this migration. Historical observations
and decisions remain in [HISTORY.md](HISTORY.md).

## Conversation recovery

George checkpoints the lab book and sends himself a handoff before a reset.
Normal resume preserves the conversation. For a fresh context, use the supported
`gc session reset <rig>/labtech.george` after saving state. Resetting the agent's
conversation does not authorize terminating an experiment.
