#!/usr/bin/env python3
"""Bounded, read-only coordinator observer for the labtech serial ledger."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from serial_queue import Queue, SUBPROCESS_PROBE_TIMEOUT, lock, read, write


ORDER_NAME = "labtech-reconcile"
STALE_AFTER = timedelta(hours=12)
NOTICE_REPEAT = timedelta(hours=24)
UNKNOWN_RETRY = timedelta(hours=12)
MAX_CITY_RIGS = 24
MAX_REVIEWERS = 8
MAX_NUDGE_RECIPIENTS = MAX_REVIEWERS + 1  # George target plus reviewers; target need not be listed.
MAX_ROWS = 100
MAX_PAUSE_CANDIDATES = 12
MAX_LINKED_DUTIES = 8
MAX_FINDINGS = 40
CONFIG_TIMEOUT = 15
STORE_TIMEOUT = 5
NUDGE_TIMEOUT = 60
ORDER_TIMEOUT_SECONDS = 900
QUEUE_RECONCILE_PROBES = 3  # ioreg + sysctl in Queue construction, then ps for an active attempt
QUEUE_RECONCILE_PROBE_SECONDS = QUEUE_RECONCILE_PROBES * SUBPROCESS_PROBE_TIMEOUT
# One config read, at most one order-history query, one pause query per rig,
# twelve root lookups, nine nudge recipients, and queue host/process probes.
MAX_EXTERNAL_COMMAND_SECONDS = (
    CONFIG_TIMEOUT
    + STORE_TIMEOUT * (1 + MAX_CITY_RIGS + MAX_PAUSE_CANDIDATES)
    + NUDGE_TIMEOUT * MAX_NUDGE_RECIPIENTS
    + QUEUE_RECONCILE_PROBE_SECONDS
)


class WatchdogError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def iso_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def field(obj, name, default=None):
    if not isinstance(obj, dict):
        return default
    camel = "".join(part[:1].upper() + part[1:] for part in name.split("_"))
    return obj.get(name, obj.get(camel, default))


def clip(value, limit=500):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def run_command(argv, timeout):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False)


def command_error(label, argv, result):
    detail = clip(result.stderr or result.stdout or f"exit {result.returncode}")
    return WatchdogError(f"{label} failed (exit {result.returncode}; {shlex.join(argv[:5])}): {detail}")


def command_json(argv, runner=run_command, timeout=CONFIG_TIMEOUT, label="command"):
    try:
        result = runner(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WatchdogError(f"{label} timed out after {timeout}s") from exc
    except OSError as exc:
        raise WatchdogError(f"{label} could not start: {exc}") from exc
    if result.returncode != 0:
        raise command_error(label, argv, result)
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WatchdogError(f"{label} returned malformed JSON: {clip(result.stdout)}") from exc


def absolute_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise WatchdogError(f"effective configuration is missing {label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WatchdogError(f"{label} must be an absolute path: {value!r}")
    return path.resolve()


def discover_context(environ=None, runner=run_command):
    environ = os.environ if environ is None else environ
    city = absolute_path(environ.get("GC_CITY_PATH"), "GC_CITY_PATH")
    rig_name = (environ.get("GC_RIG") or "").strip()
    if not rig_name:
        raise WatchdogError("controller did not provide GC_RIG; refusing to guess the importing rig")
    pack_dir = absolute_path(environ.get("PACK_DIR") or environ.get("GC_PACK_DIR"), "PACK_DIR")
    gc_bin = shutil.which("gc")
    if not gc_bin:
        raise WatchdogError("gc executable is not available on PATH")

    config_result = command_json(
        [gc_bin, "--city", str(city), "config", "show", "--json"],
        runner=runner, timeout=CONFIG_TIMEOUT, label="effective city configuration",
    )
    city_config = config_result.get("config") if isinstance(config_result, dict) else None
    rigs = field(city_config, "rigs")
    if not isinstance(rigs, list):
        raise WatchdogError("effective city configuration has no usable rig list")
    rig_config = next((item for item in rigs if field(item, "name") == rig_name), None)
    if rig_config is None:
        raise WatchdogError(f"GC_RIG {rig_name!r} is absent from effective city configuration")
    formula_vars = field(rig_config, "formula_vars", {})
    if not isinstance(formula_vars, dict):
        raise WatchdogError(f"effective configuration for rig {rig_name!r} has no formula_vars map")

    home = absolute_path(formula_vars.get("labtech_home"), "rig formula_vars.labtech_home")
    configured_pack = absolute_path(formula_vars.get("labtech_pack"), "rig formula_vars.labtech_pack")
    lab_book = (formula_vars.get("lab_book") or "").strip()
    if not lab_book:
        raise WatchdogError("effective configuration is missing rig formula_vars.lab_book")
    if configured_pack != pack_dir:
        raise WatchdogError(
            f"installed PACK_DIR {pack_dir} does not match effective labtech_pack {configured_pack}"
        )
    for required in (pack_dir / "bin" / "serial_queue.py", pack_dir / "docs" / "serial-queue.md"):
        if not required.is_file():
            raise WatchdogError(f"installed labtech pack is missing required file: {required}")

    config_path = home / "queue" / "config.json"
    if not config_path.is_file():
        raise WatchdogError(f"existing queue configuration is missing: {config_path}; observer will not initialize it")
    try:
        queue_config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchdogError(f"cannot read existing queue configuration {config_path}: {exc}") from exc
    if absolute_path(queue_config.get("city"), "queue config city") != city:
        raise WatchdogError("queue config city does not match GC_CITY_PATH")
    if queue_config.get("rig") != rig_name:
        raise WatchdogError("queue config rig does not match GC_RIG")
    if queue_config.get("lab_book") != lab_book:
        raise WatchdogError("queue config lab_book does not match effective formula_vars.lab_book")
    target = (queue_config.get("target") or "").strip()
    if not target or not target.startswith(rig_name + "/"):
        raise WatchdogError("queue config target is not scoped to the importing rig")
    reviewers = queue_config.get("reviewers")
    if not isinstance(reviewers, list) or any(not isinstance(item, str) or not item.strip() for item in reviewers):
        raise WatchdogError("queue config reviewers must be a list of configured session addresses")
    if len(reviewers) > MAX_REVIEWERS:
        raise WatchdogError(f"queue config has more than the bounded {MAX_REVIEWERS} reviewers")
    coordinator_targets = sorted({item.strip() for item in reviewers if item.strip() != target})
    rig_names = [field(item, "name") for item in rigs]
    if any(not isinstance(name, str) or not name.strip() for name in rig_names):
        raise WatchdogError("effective city configuration contains a rig without a name")
    if len(rig_names) > MAX_CITY_RIGS:
        raise WatchdogError(
            f"effective city has {len(rig_names)} rigs; reviewer scan is bounded at {MAX_CITY_RIGS}"
        )
    if rig_name not in rig_names:
        raise WatchdogError(f"importing rig {rig_name!r} is not present in the effective rig list")

    return {
        "city": city,
        "rig": rig_name,
        "pack": pack_dir,
        "home": home,
        "lab_book": lab_book,
        "gc": absolute_path(queue_config.get("gc"), "queue config gc executable"),
        "target": target,
        "coordinators": coordinator_targets,
        "rig_names": rig_names,
        "queue_config": queue_config,
    }


def rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("issues", "items", "beads", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    raise WatchdogError("bead query returned an unexpected JSON shape")


def run_bd_list(context, rig, args, runner):
    argv = [str(context["gc"]), "--city", str(context["city"]), "--rig", rig, "bd", "list", *args]
    return rows(command_json(argv, runner=runner, timeout=STORE_TIMEOUT,
                             label=f"bounded bead query for rig {rig}"))


def run_bd_show(context, rig, bead_id, runner):
    argv = [str(context["gc"]), "--city", str(context["city"]), "--rig", rig,
            "bd", "show", bead_id, "--json"]
    value = command_json(argv, runner=runner, timeout=STORE_TIMEOUT,
                         label=f"coordinator/root lookup for {rig}/{bead_id}")
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict) and value.get("id") == bead_id:
        return value
    raise WatchdogError(f"coordinator/root lookup for {rig}/{bead_id} returned no exact bead")


def finding_key(finding):
    parts = [finding.get("type", "observer"), finding.get("rig", "city"),
             finding.get("bead_id", finding.get("attempt_id", finding.get("component", "unknown")))]
    return ":".join(str(part).replace(":", "_") for part in parts)


def order_findings(context, target, now, runner):
    findings = []
    errors = []
    label = f"order-run:{ORDER_NAME}:rig:{context['rig']}"
    try:
        candidates = run_bd_list(context, context["rig"], [
            "--label", label, "--status", "open,in_progress", "--include-ephemeral",
            "--brief", "--max-rows", str(MAX_ROWS), "--json",
        ], runner)
    except WatchdogError as exc:
        message = f"cannot inspect scoped {ORDER_NAME} run history: {exc}"
        errors.append({"component": "order-history", "rig": context["rig"], "message": message})
        findings.append({"type": "store_read_error", "severity": "coordinator",
                         "component": "order-history", "rig": context["rig"], "message": message})
        return findings, errors
    if len(candidates) >= MAX_ROWS:
        message = (f"scoped {ORDER_NAME} history reached the {MAX_ROWS}-row scan bound; "
                   "results may be incomplete")
        errors.append({"component": "order-history", "rig": context["rig"], "message": message})
        findings.append({"type": "store_read_error", "severity": "coordinator",
                         "component": "order-history", "rig": context["rig"], "message": message})

    for bead in candidates:
        if not isinstance(bead, dict) or bead.get("status") != "open":
            continue
        assignee = bead.get("assignee") or ""
        if assignee not in ("", target):
            continue
        created = iso_time(bead.get("created_at"))
        if created is None:
            errors.append({"component": "order-history", "rig": context["rig"],
                           "message": f"open reconciliation bead {bead.get('id', '?')} has no parseable created_at"})
            continue
        age = now - created
        if age < STALE_AFTER:
            continue
        findings.append({
            "type": "stale_reconciliation", "severity": "coordinator" if age >= NOTICE_REPEAT else "george",
            "rig": context["rig"], "bead_id": bead.get("id"), "title": bead.get("title", ""),
            "status": bead.get("status"), "assignee": assignee or None,
            "created_at": stamp(created), "age_seconds": int(age.total_seconds()),
            "message": "scheduled reconciliation root remains open and unclaimed",
        })
    return findings, errors


def coordinator_binding(metadata, target):
    metadata = metadata or {}
    for key in ("coordinator", "gc.coordinator", "gasvillage.coordinator", "gasvillage.reviewer"):
        if metadata.get(key) == target:
            return key
    return None


def linked_review_binding(context, rig, bead, target, runner, root_budget):
    metadata = bead.get("metadata") or {}
    direct = coordinator_binding(metadata, target)
    if direct:
        return {"basis": "pause_bead", "coordinator_bead_id": bead.get("id"), "metadata_key": direct}, None

    root_id = metadata.get("gc.root_bead_id") or bead.get("parent_id")
    if not root_id:
        return None, None
    root_store = metadata.get("gc.root_store_ref") or ""
    root_rig = root_store[4:] if root_store.startswith("rig:") else rig
    if root_rig not in context["rig_names"]:
        return None, f"pause bead {rig}/{bead.get('id')} points to unknown root store {root_store!r}"
    if root_budget[0] >= MAX_PAUSE_CANDIDATES:
        return None, f"pause root lookup bound {MAX_PAUSE_CANDIDATES} reached; additional held work was not inspected"
    root_budget[0] += 1
    try:
        root = run_bd_show(context, root_rig, str(root_id), runner)
    except WatchdogError as exc:
        return None, f"cannot inspect durable root {root_rig}/{root_id} for blocked pause {rig}/{bead.get('id')}: {exc}"
    owner = coordinator_binding(root.get("metadata"), target)
    if owner:
        return {"basis": "root_coordinator", "root_bead_id": root_id,
                "coordinator_bead_id": root.get("id"), "metadata_key": owner}, None

    # Some coordinators record ownership on a linked duty under the workflow
    # root. Inspect only the bounded links included in the root's durable record.
    linked = []
    for collection in (root.get("dependencies") or [], root.get("dependents") or []):
        if isinstance(collection, list):
            linked.extend(item for item in collection if isinstance(item, dict))
    if len(linked) > MAX_LINKED_DUTIES:
        return None, (f"durable root {root_rig}/{root_id} has {len(linked)} linked duties; "
                      f"the bounded scan stops at {MAX_LINKED_DUTIES}, so coordinator ownership is unresolved")
    for item in linked:
        owner = coordinator_binding(item.get("metadata"), target)
        if owner:
            return {"basis": "linked_duty", "root_bead_id": root_id,
                    "coordinator_bead_id": item.get("id"), "metadata_key": owner}, None
    return None, None


def blocked_review_findings(context, target, now, runner):
    findings = []
    errors = []
    root_budget = [0]
    pause_count = 0
    for rig in context["rig_names"]:
        try:
            candidates = run_bd_list(context, rig, [
                "--status", "blocked", "--metadata-field", "gasvillage.pause_state=awaiting_review",
                "--brief", "--max-rows", str(MAX_ROWS), "--json",
            ], runner)
        except WatchdogError as exc:
            message = f"cannot inspect blocked George-review work in rig {rig}: {exc}"
            errors.append({"component": "review-scan", "rig": rig, "message": message})
            findings.append({"type": "store_read_error", "severity": "coordinator",
                             "component": "review-scan", "rig": rig, "message": message})
            continue
        if len(candidates) >= MAX_ROWS:
            message = (f"blocked pause query in rig {rig} reached the {MAX_ROWS}-row scan bound; "
                       "results may be incomplete")
            errors.append({"component": "review-scan", "rig": rig, "message": message})
            findings.append({"type": "store_read_error", "severity": "coordinator",
                             "component": "review-scan", "rig": rig, "message": message})
        for bead in candidates:
            if not isinstance(bead, dict) or bead.get("status") != "blocked":
                continue
            metadata = bead.get("metadata") or {}
            if metadata.get("gasvillage.pause_state") != "awaiting_review":
                continue
            pause_count += 1
            if pause_count > MAX_PAUSE_CANDIDATES:
                message = f"blocked pause scan exceeded the bounded {MAX_PAUSE_CANDIDATES}-bead coordinator/root limit"
                errors.append({"component": "review-scan", "rig": rig, "message": message})
                findings.append({"type": "store_read_error", "severity": "coordinator",
                                 "component": "review-scan", "rig": rig, "message": message})
                return findings, errors
            binding, binding_error = linked_review_binding(context, rig, bead, target, runner, root_budget)
            if binding_error:
                errors.append({"component": "review-root", "rig": rig, "bead_id": bead.get("id"),
                               "message": binding_error})
                findings.append({"type": "store_read_error", "severity": "coordinator",
                                 "component": "review-root", "rig": rig, "bead_id": bead.get("id"),
                                 "message": binding_error})
                continue
            if not binding:
                continue
            created = iso_time(bead.get("created_at"))
            age = now - created if created else timedelta(0)
            findings.append({
                "type": "blocked_review", "severity": "coordinator" if age >= NOTICE_REPEAT else "george",
                "rig": rig, "bead_id": bead.get("id"), "title": bead.get("title", ""),
                **binding,
                "status": "blocked", "assignee": bead.get("assignee"),
                "created_at": stamp(created) if created else None,
                "updated_at": bead.get("updated_at"), "age_seconds": int(age.total_seconds()),
                "pause_state": metadata.get("gasvillage.pause_state"),
                "review_condition": metadata.get("gasvillage.review_condition"),
                "message": "original blocked pause is linked to a durable George coordinator, root, or duty record",
            })
    return findings, errors


def receipt_findings(queue, checkpoint):
    findings = []
    active = checkpoint.get("active") if isinstance(checkpoint, dict) else None
    active_id = active.get("attempt_id") if isinstance(active, dict) else None
    attempts_dir = queue.root / "attempts"
    if not attempts_dir.exists():
        return findings
    for folder in sorted(attempts_dir.iterdir()):
        if not folder.is_dir():
            continue
        aid = folder.name
        record_path = folder / "record.json"
        exit_path = folder / "exit.json"
        if exit_path.exists():
            continue
        if aid == active_id:
            observation = (active or {}).get("observation", "")
            if not observation.startswith("uncertain/interrupted"):
                # A live process without an exit receipt is expected; quiet logs are not failure.
                continue
        findings.append({
            "type": "missing_run_receipt", "severity": "coordinator", "rig": queue.config.get("rig"),
            "attempt_id": aid, "record_path": str(record_path) if record_path.exists() else None,
            "active": aid == active_id,
            "observation": (active or {}).get("observation") if aid == active_id else "non-active attempt has no exit receipt",
            "message": "retain the serial slot and original evidence for coordinator review",
        })
    return findings


def unique_findings(items):
    result = {}
    for item in items:
        if not item.get("bead_id") and item.get("type") in ("stale_reconciliation", "blocked_review"):
            item = {**item, "message": item.get("message", "") + "; bead ID missing in query result"}
        key = finding_key(item)
        result.setdefault(key, {**item, "key": key})
    if len(result) > MAX_FINDINGS:
        raise WatchdogError(f"observer found {len(result)} findings; bounded report limit is {MAX_FINDINGS}")
    return result


def read_state(path):
    if not path.exists():
        return {"schema_version": 1, "findings": {}, "resolved": []}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchdogError(f"cannot read observer state {path}: {exc}; preserving it unchanged") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("findings"), dict):
        raise WatchdogError(f"observer state {path} has an unsupported shape; preserving it unchanged")
    if not isinstance(value.get("resolved", []), list):
        raise WatchdogError(f"observer state {path} has an invalid resolved history; preserving it unchanged")
    return value


def notice_due(record, recipient, at):
    previous = (record.get("notifications") or {}).get(recipient)
    if not previous:
        return True
    attempted = iso_time(previous.get("attempted_at"))
    if attempted is None:
        return True
    if previous.get("status") == "queued_unverified":
        # Do not enqueue duplicates while a prior immediate nudge still has no
        # delivery receipt. Keep reporting it and escalate through coordinator.
        return False
    delay = UNKNOWN_RETRY if previous.get("status") in ("unknown", "failed", "pending") else NOTICE_REPEAT
    return at - attempted >= delay


def add_delivery_note(finding, note):
    previous = finding.get("delivery_outcome", "")
    notes = [part for part in previous.split("; ") if part]
    if note not in notes:
        notes.append(note)
    finding["delivery_outcome"] = "; ".join(notes)


def render_message(recipient, target, findings, at, state_path):
    role = "George" if recipient == target else "configured coordinator/reviewer"
    lines = [f"Labtech 12-hour observer: {len(findings)} unresolved finding(s) for {role} at {stamp(at)}."]
    for finding in findings[:10]:
        bead = finding.get("bead_id") or finding.get("attempt_id") or finding.get("component") or "observer"
        where = f"{finding.get('rig')}/{bead}" if finding.get("rig") else str(bead)
        detail = finding.get("title") or finding.get("message") or finding.get("type")
        lines.append(f"- {finding.get('type')}: {where} — {clip(detail, 180)}")
    if len(findings) > 10:
        lines.append(f"- {len(findings) - 10} more finding(s); inspect {state_path} for the bounded receipt.")
    delivery_notes = [item.get("delivery_outcome") for item in findings if item.get("delivery_outcome")]
    if delivery_notes:
        lines.append("Prior reminder outcome: " + "; ".join(sorted(set(delivery_notes))[:3]))
    lines.append("Please inspect George's current hook, mail, and obligations. Record any scope or changed-condition review on the original bead; this observer does not unblock, resume, approve, retry, or launch work.")
    lines.append("The immediate native delivery result is retained; delivery does not prove the recipient read or acted on it.")
    return "\n".join(lines)


def send_nudge(context, recipient, message, runner):
    argv = [str(context["gc"]), "--city", str(context["city"]), "session", "nudge",
            recipient, message, "--delivery", "immediate", "--json"]
    try:
        result = runner(argv, timeout=NUDGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"attempted_at": None, "status": "unknown", "error": f"nudge timed out after {NUDGE_TIMEOUT}s"}
    except OSError as exc:
        return {"attempted_at": None, "status": "unknown", "error": f"nudge could not start: {exc}"}
    if result.returncode != 0:
        return {"attempted_at": None, "status": "unknown",
                "error": clip(result.stderr or result.stdout or f"exit {result.returncode}"),
                "returncode": result.returncode}
    try:
        native_result = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"attempted_at": None, "status": "unknown",
                "error": f"immediate nudge returned malformed JSON: {clip(result.stdout)}",
                "stderr": clip(result.stderr)}
    if not isinstance(native_result, dict) or native_result.get("ok") is not True:
        return {"attempted_at": None, "status": "unknown", "native_result": native_result,
                "error": "immediate nudge did not confirm success", "stderr": clip(result.stderr)}
    if native_result.get("outcome") == "delivered" and native_result.get("queued") is False:
        status = "delivered_unverified"
    elif native_result.get("queued") is True:
        status = "queued_unverified"
    else:
        status = "unknown"
    return {"attempted_at": None, "status": status, "native_result": native_result,
            "stderr": clip(result.stderr)}


def run_observer(environ=None, runner=run_command, queue_factory=Queue, at=None):
    now = iso_time(at) or utc_now()
    context = discover_context(environ=environ, runner=runner)
    try:
        queue = queue_factory(str(context["home"]))
    except Exception as exc:
        raise WatchdogError(f"cannot open existing serial queue: {type(exc).__name__}: {exc}") from exc
    if Path(queue.root).resolve() != (context["home"] / "queue").resolve():
        raise WatchdogError("queue object resolved to an unexpected home")
    if queue.config.get("city") != str(context["city"]) or queue.config.get("rig") != context["rig"]:
        raise WatchdogError("queue object city/rig changed after configuration validation")

    try:
        with lock(queue.root / "watchdog.lock", blocking=False):
            return _run_locked(context, queue, runner, now)
    except BlockingIOError:
        return {"status": "skipped", "reason": "another observer holds watchdog.lock",
                "effects": {"experiments_started": 0, "approvals": 0, "retries": 0}}


def _run_locked(context, queue, runner, now):
    errors = []
    findings = []
    try:
        checkpoint = queue.reconcile()
    except Exception as exc:
        checkpoint = {}
        message = f"serial queue reconcile failed: {type(exc).__name__}: {clip(exc)}"
        errors.append({"component": "queue-reconcile", "message": message})
        findings.append({"type": "observer_error", "severity": "coordinator",
                         "component": "queue-reconcile", "rig": context["rig"], "message": message})

    new_findings, order_errors = order_findings(context, context["target"], now, runner)
    findings.extend(new_findings)
    errors.extend(order_errors)
    new_findings, review_errors = blocked_review_findings(context, context["target"], now, runner)
    findings.extend(new_findings)
    errors.extend(review_errors)
    findings.extend(receipt_findings(queue, checkpoint))

    if not context["coordinators"] and any(item.get("severity") == "coordinator" for item in findings):
        message = "queue config has no reviewer other than the George target; coordinator escalation is unavailable"
        errors.append({"component": "reviewers", "message": message})
        findings.append({"type": "observer_error", "severity": "george", "component": "reviewers",
                         "rig": context["rig"], "message": message})
    current = unique_findings(findings)
    state_path = queue.root / "watchdog-state.json"
    state = read_state(state_path)
    previous = state["findings"]
    resolved = list(state.get("resolved", []))
    for key, record in previous.items():
        if key not in current:
            resolved.append({"key": key, "cleared_at": stamp(now),
                             "finding": record.get("finding", {})})
    resolved = resolved[-100:]

    active = {}
    for key, finding in current.items():
        old = previous.get(key, {})
        active[key] = {
            "finding": finding,
            "first_seen_at": old.get("first_seen_at", stamp(now)),
            "last_seen_at": stamp(now),
            "notifications": dict(old.get("notifications") or {}),
        }
        for recipient, prior_delivery in active[key]["notifications"].items():
            prior_status = prior_delivery.get("status")
            if prior_status == "delivered_unverified" or notice_due(active[key], recipient, now):
                continue
            note = f"{recipient} immediate nudge outcome remains {prior_status}"
            add_delivery_note(active[key]["finding"], note)
            if recipient == context["target"]:
                active[key]["finding"]["severity"] = "coordinator"
            errors.append({"component": "nudge", "target": recipient,
                           "status": prior_status,
                           "message": ("prior immediate nudge remains queued; duplicate delivery was suppressed"
                                       if prior_status == "queued_unverified" else
                                       "prior immediate nudge outcome is unresolved; retry remains rate-limited")})

    recipients = [context["target"], *context["coordinators"]]
    due_by_recipient = {recipient: [] for recipient in recipients}
    for key, record in active.items():
        finding = record["finding"]
        for recipient in recipients:
            if recipient != context["target"] and finding.get("severity") != "coordinator":
                continue
            if notice_due(record, recipient, now):
                due_by_recipient[recipient].append(key)

    new_state = {"schema_version": 1, "updated_at": stamp(now), "findings": active, "resolved": resolved}
    # Persist intent before external nudges. A crash during delivery is treated as
    # unknown on the next tick, avoiding an immediate duplicate message.
    for recipient, keys in due_by_recipient.items():
        if not keys:
            continue
        for key in keys:
            active[key]["notifications"][recipient] = {"attempted_at": stamp(now), "status": "pending"}
    write(state_path, new_state)

    notifications = []
    for recipient, keys in due_by_recipient.items():
        if not keys:
            continue
        selected = [active[key]["finding"] for key in keys]
        outcome = send_nudge(context, recipient,
                             render_message(recipient, context["target"], selected, now, state_path), runner)
        outcome["attempted_at"] = stamp(now)
        notifications.append({"target": recipient, **outcome, "finding_keys": keys})
        for key in keys:
            active[key]["notifications"][recipient] = dict(outcome)
        if outcome["status"] != "delivered_unverified":
            errors.append({"component": "nudge", "target": recipient, "status": outcome["status"],
                           "message": outcome.get("error", "immediate nudge did not confirm delivery")})
            for key in keys:
                add_delivery_note(active[key]["finding"],
                                  f"{recipient} immediate nudge outcome: {outcome['status']}")
        if recipient == context["target"] and outcome["status"] != "delivered_unverified":
            # Surface first failed/queued George delivery immediately to each
            # configured coordinator, subject to that recipient's own rate limit.
            for key in keys:
                active[key]["finding"]["severity"] = "coordinator"
                for coordinator in context["coordinators"]:
                    if notice_due(active[key], coordinator, now) and key not in due_by_recipient[coordinator]:
                        due_by_recipient[coordinator].append(key)
                        active[key]["notifications"][coordinator] = {
                            "attempted_at": stamp(now), "status": "pending"
                        }
        write(state_path, new_state)

    return {
        "status": "error" if errors else "observed",
        "observed_at": stamp(now),
        "rig": context["rig"],
        "lab_book": context["lab_book"],
        "queue_checkpoint": checkpoint,
        "findings": list(current.values()),
        "notifications": notifications,
        "errors": errors,
        "state_path": str(state_path),
        "effects": {"queue_reconciled": bool(checkpoint), "experiments_started": 0,
                    "approvals": 0, "retries": 0, "held_work_mutated": 0},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = run_observer()
    except WatchdogError as exc:
        result = {"status": "error", "errors": [{"component": "configuration", "message": str(exc)}],
                  "next_action": "inspect effective city config, installed PACK_DIR, and existing queue config; do not initialize or replace queue state"}
    except Exception as exc:
        result = {"status": "error", "errors": [{"component": "observer", "message": f"{type(exc).__name__}: {clip(exc)}"}],
                  "next_action": "inspect retained queue and order evidence; do not launch or retry scientific work"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in ("observed", "skipped") and not result.get("errors") else 2


if __name__ == "__main__":
    sys.exit(main())
