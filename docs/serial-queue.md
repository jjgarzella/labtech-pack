# George's serial queue

`labtech-queue` is a short formula executed by the existing persistent George
session. It reconciles first, reviews explicitly submitted requests, starts at
most one approved request, checkpoints, and returns to conversation and other
assigned work. It never drains George or replaces his other coordination duties.
The new `labtech-reconcile` order runs every **8 hours**, routed to that same
session. Reconciliation only observes and checkpoints; it cannot launch, approve,
or retry. Request and completion nudges provide prompt service between ticks.

The legacy `labtech-tick` formula/order is reference-only for this workflow. Do
not enable it: it selects historical goals and changes scientific parameters.
Neither this installation nor submission resumes held historical work or grants
FunSearch authorization. Old lab books, IDs, pidfiles and logs remain untouched.

## Initialize once per served rig

Use Python 3.9+ on macOS or Linux; catalog resolution separately needs Python 3.11+
(`tomllib`). Run with absolute paths. The local filesystem must implement POSIX
flock and atomic replace/fsync. A queue is **one host only**; do not put it on NFS,
copy it to a second machine and run there, or bypass it with another dispatcher.
Remote execution needs a separately reviewed host-side ownership/receipt protocol.

```sh
python3 "$LABTECH_PACK/bin/serial_queue.py" --home "$LABTECH_HOME" init \
  --city "$GC_CITY" --rig math-experiments \
  --target math-experiments/labtech.george --lab-book mx-p03 \
  --reviewer gasvillage.mayor --reviewer math-experiments/labtech.george
```

`queue/config.json` pins the machine identity, served rig, explicit gc executable,
George address, lab book and permitted reviewing actors. Initialize is explicit;
reconciliation will not invent or replace a configuration. Existing config refuses
reinitialization. Do not edit config/ledger to clear an occupied slot.

## Submit, review, execute

Create an ordinary persistent request task in the served rig, parented to the
existing lab book and labeled `labtech-request`. Use the task ID as request ID.
Its text names the scope and actual authorization; it is not a new grant itself.
Other agents submit with `submit --request ID --spec /absolute/spec.json`.
Submission seals input/executable hashes, stores the immutable spec and stable
request/run IDs, links the bead to the ledger, and nudges George. Resubmitting the
same ID/spec is idempotent. A different spec requires a **new request**.

A spec is JSON with these required fields (replace every example value):

```json
{
  "purpose": "Exact authorized experiment and measurement plan",
  "worktree_owner": "agent owning this clean, isolated source checkout",
  "argv": ["/absolute/runtime", "--project=/absolute/owned/worktree", "/absolute/experiment.jl"],
  "cwd": "/absolute/owned/worktree",
  "source": {"path": "/absolute/owned/worktree", "revision": "full git HEAD"},
  "runtime": {
    "argv": ["/absolute/runtime", "--version"],
    "expected": "exact approved runtime version"
  },
  "parameters": {"seed": 42, "sample_count": 1},
  "inputs": ["/absolute/experiment.jl", "/absolute/owned/worktree/Project.toml", "/absolute/owned/worktree/Manifest.toml"],
  "scope_beads": [{"rig": "source-rig", "id": "original-task-id"}],
  "env": {"JULIA_NUM_THREADS": "1"}
}
```

Use exact argv, not an implicitly interpreted command string. Explicit
`["/bin/bash", "-c", "..."]` is possible but review all quoting and input scripts.
List every relevant script, input dataset, project, Manifest, recipe, preferences,
and non-source runtime file in `inputs`. Source HEAD must match the approved
revision and the checkout must be clean, including untracked files. Ignored
runtime/configuration files still need explicit input evidence. Input/executable
hashes are sealed on submission and rechecked before execution. Use a separate
owned worktree when developing source; don't freeze or edit another actor's tree.
Runtime probe argv is also authorized code and must be harmless and bounded. For
Julia runs, capture VERSION, active project and loaded dependency sources/image
in the experiment's log; a version string alone cannot prove source/image identity.

`env` records scientific/runtime knobs; credentials are loaded by the rig's
credential seam and never put in spec/argv. The inherited environment's key names
are recorded, not secret values. Pin relevant environment values in `env` or
record a non-secret file/hash evidence reference. Snapshot only non-secret inputs;
never add a credential file to `inputs`. Arbitrary external mutable state is not
made reproducible by a git revision: include the actual data/seed/DB snapshot
references in the authorized request and results.

The executable must remain **foreground** until its experiment finishes. The
supervisor supplies `LABTECH_REQUEST_ID`, `LABTECH_RUN_ID`, `LABTECH_ATTEMPT_ID`
and an absolute `LABTECH_RESULTS_DIR`. Write raw results there, including an
`outcome.json` containing the measured outcome and its evidence/units. For a DB
experiment include a saved before/after snapshot and the goal's baseline/filter/
uniqueness-key computation; never confuse zero rows, an error, and no measurement.
Do not escape the supervisor's group with setsid, daemonize, submit a remote job,
or use asynchronous Kaimon evaluation as the queued command. Those need a reviewed
foreground adapter that waits for the real job and durable results.

George reviews the original authorization, current dependencies and pause records,
exact inputs/source/runtime and measurement plan. Approval is recorded with the
spec digest in the request bead and on disk:

```sh
python3 "$LABTECH_PACK/bin/serial_queue.py" --home "$LABTECH_HOME" approve \
  --request REQUEST --authorization-id AUTHORIZATION --authorization-rig SOURCE_RIG
python3 "$LABTECH_PACK/bin/serial_queue.py" --home "$LABTECH_HOME" run-next
```

Only the configured George identity (`GC_AGENT`) may `run-next`; configured
reviewers may approve/retry/dispose interruptions. Identity checks complement the
normal local trusted workspace and bead permissions; this is not a security
sandbox for malicious shell code. All submitters/reviewers must preserve the
original authorization and holds; a claimed approval is not proof of scope.
The runner rechecks bead approval, original authorization and all scope beads for
blocked/deferred states, unresolved blocking dependencies and explicit hold flags
before reservation and immediately before the experiment starts. Authorization
remains a coordinator judgment; bead text cannot be automatically interpreted.

Serve the formula via the supported routing/claim path, or have George execute its
compiled instructions in response to the nudge. Do not spawn a second George.

## Ledger and serial ownership

`queue/requests/<request>/` holds immutable `spec.json`, initial IDs and submitter,
approval, mutable request lifecycle checkpoint, and notification receipt.
`queue/attempts/<run>.aN/` retains **every attempt** including failures and retries:

- `record.json`: request/run/attempt IDs, previous attempt, owner, source/runtime/
  parameters/spec and approval, host/boot identity and artifact/log locations.
  It is fsynced **before supervisor launch**; the bead launch-owner/path link is
  written before launch too.
- `launch.json`, `identity.json`, `child.json`: launcher/supervisor/child process
  evidence, machine+boot+PID+process-start/command identity. Host migration is never
  interpreted as a local process exit.
- `provenance.json`, `source.patch`, `inputs/`: actual command argv/display/cwd,
  runtime probe/output, source revision/status and input hashes/copies, explicit
  environment knobs. These precede experiment launch. `start.json` records start.
- `stdout.log`, `stderr.log`, `supervisor.log`, `results/`: retained raw evidence.
- `exit.json`: durable end time, exact subprocess return code (negative means
  signal), errors, measured outcome or explicit missing evidence, artifact hashes.
  It precedes the completion nudge. `notification.json` retains delivery failures.

`active.json` reserves the serial slot durably. `serial.lock` is an OS lock held by
the launcher and inherited by supervisor/foreground child. Never unlink either
lock file. A detached Python supervisor uses `start_new_session=True` (no external
`setsid` dependency on Mac) and strips GC session identity markers so agent teardown
does not own the scientific process. It waits for the foreground child and ordinary
same-group descendants before receipt/release. This serializes queued experiments
across George restarts, distinct from `max_active_sessions=1`.

`reconcile` checks receipts/identity and writes `checkpoint.json`. It releases a
finished reservation only with a matching receipt **and** an available process
lock. It never launches or retries. Missing receipt or mismatched identity retains
the reservation as uncertain/interrupted. Quiet logs stay running; an absent PID
alone never establishes success. If gc/beads or source/provenance writes fail before
launch, fail closed; evidence may require review before further work. Do not
restart shared services to repair queue state.

After normal completion, George reviews the measured outcome, updates the request
bead with status/evidence and appends a concise lab-book digest. `completed` in the
queue means process exited zero, not scientific success or request closure. Missing
measurements remain visible for follow-up. The lab book and conversation are not
the run ledger.

## Failure, interruption and retries

Nonzero exits and preflight failures become failed attempts. No automatic retries.
To retry a failed request, record changed conditions and authorization on the
original request/authorization bead, then call `retry` with the same authorization
flags as `approve`. Each new attempt keeps the run ID and links the previous attempt;
completed work cannot be retried through this command. Changed scientific inputs
need a new request/spec. A scope change is never an implicit retry.

For an interrupted supervisor without a receipt, first establish the fate of the
owned process and descendants; never signal or reap an unrelated/reused PID. Record
the reason, process/host/boot evidence and coordinator disposition on the original
bead. `resolve-interrupted --attempt ID --authorization-id ID --authorization-rig
RIG --evidence-file /absolute/review.txt` requires a configured reviewer, an
available serial lock and no remaining known supervisor/process group. It writes
a retained interruption review and an **unknown exit status**, not fabricated
success, then reconciles. A fresh explicit retry still needs a separate command.
Never resolve an explicit work pause by waking a new incarnation or editing queue
state: use the city's original-bead coordinator review/resume workflow first.

Legacy rig dispatch/ping adapters must use this ledger or fail closed. Historical
v1 pidfiles have no machine/start/receipt identity; retain them as evidence. Never
silently adopt/delete them or use their PIDs to determine current-host liveness.

## Validation

`python3 -m unittest discover -s tests -v` uses disposable git repos and fake beads,
with real detached harmless children. It covers serialization across new Queue
instances, failed/retried attempts and links, receipt/outcome evidence, pending
review and explicit holds, input/source changes, cross-host refusal and a killed
supervisor with a surviving foreground child. Live acceptance is one bounded
harmless request through George; no scientific experiment is implied.
