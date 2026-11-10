#!/usr/bin/env python3
"""Durable, local-host, foreground experiment queue. See docs/serial-queue.md."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    """Publish complete JSON and fsync both contents and directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("xb") as out:
        out.write(encoded(value))
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def label(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("invalid id: " + value)
    return value


def capture(argv, cwd=None, env=None):
    return subprocess.check_output(argv, cwd=cwd, env=env, stderr=subprocess.STDOUT,
                                   timeout=30).decode(errors="replace").strip()


@lru_cache(maxsize=1)
def host():
    if sys.platform == "darwin":
        identity = capture(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        identity = re.search(r'"IOPlatformUUID" = "([^"]+)"', identity).group(1)
        boot = capture(["/usr/sbin/sysctl", "-n", "kern.boottime"])
    else:
        identity = Path("/etc/machine-id").read_text().strip()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"machine": identity, "boot": boot, "hostname": socket.getfqdn(),
            "platform": platform.platform()}


def process(pid):
    try:
        return capture(["ps", "-p", str(pid), "-o", "lstart=,pgid=,command="])
    except subprocess.CalledProcessError:
        return None


def group_members(pgid, exclude):
    probe = subprocess.Popen(["ps", "-axo", "pid=,pgid=,stat="], stdout=subprocess.PIPE)
    output, _ = probe.communicate(timeout=30)
    if probe.returncode:
        raise ValueError("could not inspect process group")
    # The ps probe is itself briefly in this group. Exclude its exact PID.
    return [int(p) for row in output.decode().splitlines() if len(parts := row.split()) == 3
            for p, g, state in [parts]
            if int(g) == pgid and int(p) not in (exclude, probe.pid) and not state.startswith("Z")]


@contextlib.contextmanager
def lock(path, blocking=True):
    # Never replace/unlink a lock inode. The supervisor inherits serial.lock.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        # close only: explicit LOCK_UN would unlock the inherited descriptor too.
        os.close(fd)


class Queue:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.root = self.home / "queue"
        self.config = read(self.root / "config.json")
        if self.config["machine"] != host()["machine"]:
            raise ValueError("queue belongs to another host; no PID-only cross-host recovery")

    def gc(self, *args, rig=None):
        return capture([self.config["gc"], "--city", self.config["city"],
                        "bd", "--rig", rig or self.config["rig"], *args])

    def bead(self, ref):
        data = json.loads(self.gc("show", ref["id"], "--json", rig=ref["rig"]))
        return data[0] if isinstance(data, list) else data

    def permitted(self, ref, request=False):
        bead = self.bead(ref)
        meta = bead.get("metadata") or {}
        if bead["status"] in ("blocked", "deferred", "tombstone"):
            raise ValueError("held/blocked bead: " + ref["id"])
        if request and bead["status"] not in ("open", "in_progress"):
            raise ValueError("request no longer open: " + ref["id"])
        for key in ("dispatch_hold", "pause_requested", "coordinator_review_required"):
            if meta.get(key) not in (None, "", False, "false", "0"):
                raise ValueError("explicit hold on " + ref["id"] + ": " + key)
        for dep in bead.get("dependencies", []):
            if dep.get("dependency_type", dep.get("type", "blocks")) == "blocks" and dep.get("status") != "closed":
                raise ValueError("unresolved dependency on " + ref["id"])
        return bead

    def notify(self, message, folder):
        try:
            output = capture([self.config["gc"], "--city", self.config["city"],
                              "session", "nudge", self.config["target"], message])
            receipt = {"at": now(), "delivered": True, "output": output}
        except Exception as exc:
            receipt = {"at": now(), "delivered": False, "error": str(exc)}
        write(Path(folder) / "notification.json", receipt)

    def request_dir(self, rid):
        return self.root / "requests" / label(rid)

    def actor(self, reviewers=False):
        actor = os.environ.get("GC_AGENT", "")
        allowed = self.config["reviewers"] if reviewers else [self.config["target"]]
        if actor not in allowed:
            raise ValueError("actor must be " + ", ".join(allowed))
        return actor

    def submit(self, rid, spec):
        self.permitted({"rig": self.config["rig"], "id": rid}, request=True)
        validate_spec(spec)
        spec = dict(spec)
        spec["input_sha256"] = {name: file_digest(name) for name in
                                dict.fromkeys([*spec["inputs"], spec["argv"][0], spec["runtime"]["argv"][0]])}
        folder = self.request_dir(rid)
        with lock(self.root / "state.lock"):
            if folder.exists():
                if read(folder / "spec.json") != spec:
                    raise ValueError("request id already has a different immutable spec")
                return read(folder / "request.json")
            folder.mkdir()
            write(folder / "spec.json", spec)
            record = {"request_id": rid, "run_id": "run-" + uuid.uuid4().hex,
                      "submitted_at": now(), "submitted_by": os.environ.get("GC_AGENT", ""),
                      "spec_sha256": digest(encoded(spec)), "state": "pending_review"}
            write(folder / "request.json", record)
            self.gc("update", rid, "--set-metadata", "labtech_request=" + str(folder),
                    "--set-metadata", "labtech_spec_sha256=" + record["spec_sha256"])
        self.notify("Labtech request " + rid + " awaits scope/hold review; run labtech-queue formula.", folder)
        return record

    def approve(self, rid, authorization):
        actor = self.actor(reviewers=True)
        self.permitted(authorization)
        self.permitted({"rig": self.config["rig"], "id": rid}, request=True)
        folder = self.request_dir(rid)
        with lock(self.root / "state.lock"):
            record = read(folder / "request.json")
            if record["state"] != "pending_review":
                raise ValueError("only pending_review requests may be approved")
            approval = {"reviewer": actor, "at": now(), "authorization": authorization,
                        "spec_sha256": record["spec_sha256"]}
            self.gc("update", rid, "--set-metadata", "labtech_approval=" + json.dumps(approval))
            write(folder / "approval.json", approval)
            record["state"] = "queued"
            write(folder / "request.json", record)
        self.notify("Authorized labtech request " + rid + " queued; run labtech-queue formula.", folder)
        return record

    def authorize(self, folder):
        request = read(folder / "request.json")
        approval = read(folder / "approval.json")
        spec = read(folder / "spec.json")
        if digest(encoded(spec)) != approval["spec_sha256"] or request["spec_sha256"] != approval["spec_sha256"]:
            raise ValueError("approved spec digest mismatch")
        bead = self.permitted({"rig": self.config["rig"], "id": request["request_id"]}, request=True)
        stored = (bead.get("metadata") or {}).get("labtech_approval")
        if isinstance(stored, str):
            stored = json.loads(stored)
        if stored != approval:
            raise ValueError("durable bead approval differs from queue approval")
        self.permitted(approval["authorization"])
        for ref in spec["scope_beads"]:
            self.permitted(ref)
        return request, spec, approval

    def attempts(self):
        return sorted((self.root / "attempts").glob("*/record.json"))

    def reconcile(self):
        """Observe and checkpoint only: never launch, retry, or infer success."""
        with lock(self.root / "state.lock"):
            active_path = self.root / "active.json"
            active = read(active_path) if active_path.exists() else None
            if active:
                folder = self.root / "attempts" / label(active["attempt_id"])
                if (folder / "exit.json").exists():
                    receipt = read(folder / "exit.json")
                    if receipt["attempt_id"] != active["attempt_id"] or receipt["token"] != active["token"]:
                        raise ValueError("receipt identity mismatch")
                    # Release requires the supervisor AND all inheritors to have closed the lock.
                    try:
                        with lock(self.root / "serial.lock", blocking=False):
                            record = read(folder / "record.json")
                            request_path = self.request_dir(record["request_id"]) / "request.json"
                            request = read(request_path)
                            request["state"] = "completed" if receipt["exit_code"] == 0 else "failed"
                            request["last_attempt_id"] = active["attempt_id"]
                            write(request_path, request)
                            active_path.unlink()
                            active = None
                    except BlockingIOError:
                        pass
                else:
                    identity_path = folder / "identity.json"
                    identity = read(identity_path) if identity_path.exists() else None
                    if identity and identity["host"] == host() and process(identity["pid"]) == identity["ps"]:
                        active["observation"] = "running; quiet logs are not failure"
                    else:
                        active["observation"] = "uncertain/interrupted; no exit receipt; manual disposition required"
            records = [{"request": read(p / "request.json"), "path": str(p)}
                       for p in sorted((self.root / "requests").glob("*")) if (p / "request.json").exists()]
            checkpoint = {"at": now(), "host": host(), "active": active, "requests": records,
                          "attempts": [str(p.parent) for p in self.attempts()]}
            write(self.root / "checkpoint.json", checkpoint)
        return checkpoint

    def run_next(self):
        owner = self.actor()
        self.reconcile()
        try:
            with lock(self.root / "serial.lock", blocking=False) as serial_fd:
                with lock(self.root / "state.lock"):
                    if (self.root / "active.json").exists():
                        return {"launched": False, "reason": "active reservation requires reconciliation/review"}
                    candidates = []
                    for path in (self.root / "requests").glob("*/request.json"):
                        req = read(path)
                        if req["state"] == "queued":
                            candidates.append((req["submitted_at"], path.parent))
                    if not candidates:
                        return {"launched": False, "reason": "queue empty"}
                    # A newly held request does not prevent unrelated approved work.
                    errors = []
                    for _, folder in sorted(candidates):
                        try:
                            request, spec, approval = self.authorize(folder)
                            break
                        except Exception as exc:
                            errors.append({"request_id": folder.name, "error": str(exc)})
                    else:
                        return {"launched": False, "held_requests": errors}
                    attempt_number = len(list((self.root / "attempts").glob(request["run_id"] + ".a*"))) + 1
                    aid = request["run_id"] + ".a" + str(attempt_number)
                    attempt = self.root / "attempts" / aid
                    attempt.mkdir(parents=True)
                    token = uuid.uuid4().hex
                    record = {"request_id": request["request_id"], "run_id": request["run_id"],
                              "attempt_id": aid, "attempt_number": attempt_number, "token": token,
                              "owner": owner, "host": host(), "prepared_at": now(),
                              "previous_attempt_id": request.get("last_attempt_id"),
                              "spec": spec, "approval": approval, "spec_sha256": request["spec_sha256"],
                              "paths": {"stdout": str(attempt / "stdout.log"), "stderr": str(attempt / "stderr.log"),
                                        "results": str(attempt / "results"), "exit": str(attempt / "exit.json")}}
                    write(attempt / "record.json", record)  # BEFORE any child launch
                    write(self.root / "active.json", {"attempt_id": aid, "token": token, "host": host()})
                    request["state"] = "running"
                    request["last_attempt_id"] = aid
                    write(folder / "request.json", request)
                    # Persist the launch ownership in beads before spawning as well.
                    self.gc("update", request["request_id"], "--set-metadata", "labtech_attempt=" + aid,
                            "--set-metadata", "labtech_record=" + str(attempt / "record.json"),
                            "--set-metadata", "labtech_launch_owner=" + owner)
                    env = {k: v for k, v in os.environ.items() if not k.startswith("GC_")}
                    with (attempt / "supervisor.log").open("ab", buffering=0) as log:
                        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--home", str(self.home),
                                                  "_supervise", "--attempt", aid, "--fd", str(serial_fd)],
                                                 env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                 start_new_session=True, pass_fds=(serial_fd,))
                    threading.Thread(target=child.wait, daemon=True).start()
                    write(attempt / "launch.json", {"at": now(), "pid": child.pid, "host": host()})
                    return {"launched": True, "attempt_id": aid, "record": str(attempt / "record.json")}
        except BlockingIOError:
            return {"launched": False, "reason": "serial process lock held"}

    def resolve_interrupted(self, aid, authorization, evidence_path):
        actor = self.actor(reviewers=True)
        self.permitted(authorization)
        evidence = Path(evidence_path).read_text()
        if not evidence.strip():
            raise ValueError("manual disposition requires concrete identity/process evidence")
        with lock(self.root / "serial.lock", blocking=False):
            with lock(self.root / "state.lock"):
                active = read(self.root / "active.json")
                if active["attempt_id"] != aid:
                    raise ValueError("attempt is not the active reservation")
                folder = self.root / "attempts" / label(aid)
                if (folder / "exit.json").exists():
                    raise ValueError("existing exit receipt must be reconciled, never replaced")
                record = read(folder / "record.json")
                if (folder / "identity.json").exists():
                    identity = read(folder / "identity.json")
                    if identity["host"] == host():
                        if process(identity["pid"]) == identity["ps"] or group_members(identity["pgid"], -1):
                            raise ValueError("owned supervisor/group still alive; no automatic kill or release")
                disposition = {"reviewer": actor, "authorization": authorization, "at": now(),
                               "evidence": evidence, "evidence_sha256": digest(evidence.encode())}
                self.gc("update", record["request_id"], "--append-notes",
                        "Interrupted attempt manual disposition: " + json.dumps(disposition))
                write(folder / "interruption-review.json", disposition)
                write(folder / "exit.json", {"attempt_id": aid, "token": record["token"],
                      "request_id": record["request_id"], "run_id": record["run_id"],
                      "host": host(), "started_at": None, "ended_at": now(), "exit_code": None,
                      "status": "interrupted", "outcome": {"measured_outcome": None},
                      "error": "exit status unavailable; manually reviewed interruption",
                      "disposition": disposition})
        return self.reconcile()

    def retry(self, rid, authorization):
        actor = self.actor(reviewers=True)
        self.permitted(authorization)
        with lock(self.root / "serial.lock", blocking=False):
            with lock(self.root / "state.lock"):
                if (self.root / "active.json").exists():
                    raise ValueError("active/uncertain run requires disposition first")
                path = self.request_dir(rid) / "request.json"
                record = read(path)
                if record["state"] != "failed":
                    raise ValueError("only a failed request may be explicitly retried")
                previous = read(self.root / "attempts" / record["last_attempt_id"] / "exit.json")
                if previous["exit_code"] == 0:
                    raise ValueError("completed work cannot be retried")
                approval = {"reviewer": actor, "at": now(), "authorization": authorization,
                            "spec_sha256": record["spec_sha256"], "retry_of": record["last_attempt_id"]}
                self.gc("update", rid, "--set-metadata", "labtech_approval=" + json.dumps(approval))
                write(path.parent / "approval.json", approval)
                record["state"] = "queued"
                write(path, record)
        self.notify("Explicit retry queued for " + rid, path.parent)
        return record


def validate_spec(spec):
    for key in ("argv", "cwd", "source", "runtime", "parameters", "inputs", "scope_beads", "purpose", "worktree_owner"):
        if key not in spec:
            raise ValueError("missing required spec field: " + key)
    if not isinstance(spec["argv"], list) or not spec["argv"] or not all(isinstance(x, str) for x in spec["argv"]):
        raise ValueError("argv must be a nonempty string array; shell commands must explicitly name bash -c")
    for argv in (spec["argv"], spec["runtime"]["argv"]):
        if not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
            raise ValueError("executable must be an existing absolute path")
    for path in (spec["cwd"], spec["source"]["path"]):
        if not Path(path).is_absolute() or not Path(path).is_dir():
            raise ValueError("cwd/source must be existing absolute directories")
    if not spec["source"].get("revision") or not spec["scope_beads"] or not spec["worktree_owner"]:
        raise ValueError("source revision, scope beads and worktree owner required")
    if not spec["runtime"].get("expected"):
        raise ValueError("runtime.expected must pin expected version/identity output")
    if not isinstance(spec["parameters"], dict):
        raise ValueError("parameters must be explicit object (empty for smoke)")
    for path in spec["inputs"]:
        if not Path(path).is_absolute() or not Path(path).is_file():
            raise ValueError("inputs must name existing absolute files")
    for key in spec.get("env", {}):
        if key.startswith("GC_") or re.search(r"SECRET|TOKEN|PASSWORD|CREDENTIAL|API_KEY|ANON_KEY", key, re.I):
            raise ValueError("identity/secret values cannot be stored in spec.env; use a rig credential seam")


def provenance(spec, folder):
    validate_spec(spec)
    source = spec["source"]["path"]
    revision = capture(["git", "-C", source, "rev-parse", "HEAD"])
    if revision != spec["source"]["revision"]:
        raise ValueError("source revision changed since authorization")
    status = capture(["git", "-C", source, "status", "--porcelain=v1", "--untracked-files=all"])
    patch = subprocess.check_output(["git", "-C", source, "diff", "HEAD", "--binary"])
    (folder / "source.patch").write_bytes(patch)
    # Dirty tracked and untracked files require explicit, reviewable input evidence.
    if status:
        raise ValueError("source is dirty; use a clean isolated pinned worktree")
    files = []
    input_dir = folder / "inputs"
    input_dir.mkdir()
    for index, name in enumerate(dict.fromkeys([*spec["inputs"], spec["argv"][0], spec["runtime"]["argv"][0]])):
        src = Path(name)
        sha = file_digest(src)
        if sha != spec["input_sha256"][name]:
            raise ValueError("input/executable changed since approval: " + name)
        # Executables are hashed, scripts/configuration/data are copied as evidence.
        copied = None
        if name in spec["inputs"]:
            copied = str(input_dir / (str(index) + "-" + src.name))
            with src.open("rb") as inp, Path(copied).open("wb") as out:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            if file_digest(copied) != sha:
                raise ValueError("input changed during snapshot: " + name)
        files.append({"path": name, "sha256": sha, "size": src.stat().st_size, "snapshot": copied})
    env = os.environ.copy()
    env.update(spec.get("env", {}))
    runtime = capture(spec["runtime"]["argv"], cwd=spec["cwd"], env=env)
    if spec["runtime"].get("expected") and spec["runtime"]["expected"] not in runtime:
        raise ValueError("runtime identity differs from approved expected output")
    return {"at": now(), "source": {"path": source, "revision": revision, "status": status,
            "patch_sha256": digest(patch)}, "runtime": {"argv": spec["runtime"]["argv"], "output": runtime},
            "inputs": files, "argv": spec["argv"], "command_display": shlex.join(spec["argv"]),
            "cwd": spec["cwd"], "parameters": spec["parameters"], "environment": spec.get("env", {}),
            "inherited_environment_keys": sorted(k for k in os.environ if not k.startswith("GC_"))}


def supervise(queue, aid, fd):
    folder = queue.root / "attempts" / label(aid)
    record = read(folder / "record.json")
    spec = record["spec"]
    inherited = os.fstat(fd)
    expected = (queue.root / "serial.lock").stat()
    if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("supervisor did not inherit the serial lock inode")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock(queue.root / "state.lock"):
        active = read(queue.root / "active.json")
        if active["attempt_id"] != aid or active["token"] != record["token"] or record["host"] != host():
            raise ValueError("supervisor reservation/host identity mismatch")
        if any((folder / name).exists() for name in ("identity.json", "start.json", "exit.json")):
            raise ValueError("attempt already started; internal supervisor replay refused")
        request = read(queue.request_dir(record["request_id"]) / "request.json")
        if request["state"] != "running" or request["last_attempt_id"] != aid:
            raise ValueError("request no longer owns this attempt")
        identity = {"pid": os.getpid(), "pgid": os.getpgrp(), "host": host(), "token": record["token"],
                    "ps": process(os.getpid()), "at": now()}
        write(folder / "identity.json", identity)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    code, error, started = None, None, None
    child_launched, quiesced = False, False
    try:
        # Recheck holds immediately before executing. No historical goal selection.
        queue.authorize(queue.request_dir(record["request_id"]))
        evidence = provenance(spec, folder)
        write(folder / "provenance.json", evidence)
        results = folder / "results"
        results.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GC_")}
        env.update(spec.get("env", {}))
        env.update(LABTECH_REQUEST_ID=record["request_id"], LABTECH_RUN_ID=record["run_id"],
                   LABTECH_ATTEMPT_ID=aid, LABTECH_RESULTS_DIR=str(results))
        # Actual argv/cwd/env and input hashes are durable BEFORE experiment Popen.
        started = now()
        write(folder / "start.json", {"at": started, "argv": spec["argv"], "cwd": spec["cwd"],
                                      "attempt_id": aid, "token": record["token"]})
        with (folder / "stdout.log").open("ab", buffering=0) as out, (folder / "stderr.log").open("ab", buffering=0) as err:
            child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, pass_fds=(fd,))
            child_launched = True
            write(folder / "child.json", {"pid": child.pid, "ps": process(child.pid), "host": host(), "at": now()})
            code = child.wait()
            # Do not release the serial slot while ordinary background descendants remain.
            while group_members(os.getpgrp(), os.getpid()):
                time.sleep(1)
            quiesced = True
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            (folder / "preflight-error.log").write_bytes(exc.output or b"")
        else:
            (folder / "preflight-error.log").write_text(error + "\n")
        if child_launched and not quiesced:
            write(folder / "supervisor-error.json", {"at": now(), "error": error,
                  "disposition": "uncertain; retain active reservation and verify descendants"})
            queue.notify("Labtech " + aid + " has uncertain child state; retained serial reservation.", folder)
            return
        code = 125 if code is None else code
    artifacts = []
    for path in sorted((folder / "results").rglob("*")):
        if path.is_file():
            artifacts.append({"path": str(path), "size": path.stat().st_size,
                              "sha256": file_digest(path)})
    outcome_path = folder / "results" / "outcome.json"
    try:
        outcome = read(outcome_path) if outcome_path.exists() else {"measured_outcome": None, "reason": "measurement pending George review"}
    except Exception as exc:
        outcome = {"measured_outcome": None, "error": str(exc)}
    write(folder / "exit.json", {"request_id": record["request_id"], "run_id": record["run_id"],
          "attempt_id": aid, "token": record["token"], "host": host(), "started_at": started,
          "ended_at": now(), "exit_code": code, "signal": -code if code is not None and code < 0 else None,
          "error": error, "artifacts": artifacts, "outcome": outcome,
          "scientific_success": "pending review; exit zero alone does not establish result"})
    # Exit receipt precedes notification; missed nudges are recovered by reconciliation.
    queue.notify("Labtech attempt " + aid + " finished; reconcile exit receipt and review measured outcome.", folder)
    os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    for name in ("city", "rig", "target", "lab-book"):
        init.add_argument("--" + name, required=True)
    init.add_argument("--gc", default=shutil.which("gc"))
    init.add_argument("--reviewer", action="append", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--request", required=True)
    submit.add_argument("--spec", required=True)
    for name in ("approve", "retry"):
        p = sub.add_parser(name)
        p.add_argument("--request", required=True)
        p.add_argument("--authorization-id", required=True)
        p.add_argument("--authorization-rig", required=True)
    interrupted = sub.add_parser("resolve-interrupted")
    interrupted.add_argument("--attempt", required=True)
    interrupted.add_argument("--authorization-id", required=True)
    interrupted.add_argument("--authorization-rig", required=True)
    interrupted.add_argument("--evidence-file", required=True)
    sub.add_parser("reconcile")
    sub.add_parser("run-next")
    supervisor = sub.add_parser("_supervise")
    supervisor.add_argument("--attempt", required=True)
    supervisor.add_argument("--fd", type=int, required=True)
    args = parser.parse_args()
    if args.command == "init":
        root = Path(args.home).resolve() / "queue"
        root.mkdir(parents=True, exist_ok=True)
        config = {"city": str(Path(args.city).resolve()), "rig": args.rig, "target": args.target,
                  "lab_book": args.lab_book, "reviewers": args.reviewer, "gc": str(Path(args.gc).resolve()),
                  "machine": host()["machine"], "created_at": now()}
        with lock(root / "state.lock"):
            if (root / "config.json").exists():
                raise ValueError("already initialized; preserve existing queue configuration")
            write(root / "config.json", config)
            (root / "requests").mkdir(exist_ok=True)
            (root / "attempts").mkdir(exist_ok=True)
        result = config
    else:
        queue = Queue(args.home)
        if args.command == "_supervise":
            supervise(queue, args.attempt, args.fd)
            return
        if args.command == "submit":
            result = queue.submit(args.request, read(args.spec))
        elif args.command in ("approve", "retry"):
            result = getattr(queue, args.command)(args.request, {"rig": args.authorization_rig, "id": args.authorization_id})
        elif args.command == "resolve-interrupted":
            result = queue.resolve_interrupted(args.attempt,
                     {"rig": args.authorization_rig, "id": args.authorization_id}, args.evidence_file)
        elif args.command == "run-next":
            result = queue.run_next()
        else:
            result = queue.reconcile()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)
