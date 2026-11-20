#!/usr/bin/env python3
"""Bounded, read-only coordinator observer for the labtech serial ledger."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from itertools import islice

from serial_queue import Queue, SUBPROCESS_PROBE_TIMEOUT, label, lock, read, write


ORDER_NAME = "labtech-reconcile"
STALE_AFTER = timedelta(hours=12)
NOTICE_REPEAT = timedelta(hours=24)
UNKNOWN_RETRY = timedelta(hours=12)
MAX_CITY_RIGS = 24
MAX_REVIEWERS = 8
MAX_NUDGE_RECIPIENTS = MAX_REVIEWERS + 1  # George target plus reviewers; target need not be listed.
MAX_ROWS = 100
MAX_OWNED_ROWS = 64
MAX_CLOSED_OWNED_ROWS = 64
MAX_REQUEST_ROWS = 64
MAX_CLOSED_REQUEST_ROWS = 64
MAX_GRAPH_NODES = 128
MAX_GRAPH_LOOKUPS = 24
MAX_GRAPH_DEPTH = 6
MAX_TRACKED_ROOTS = 12
MAX_PAUSE_CANDIDATES = 12
MAX_LINKED_DUTIES = 8
MAX_ATTEMPT_ROWS = 100
MAX_FINDINGS = 40
CONFIG_TIMEOUT = 15
STORE_TIMEOUT = 3
NUDGE_TIMEOUT = 60
ORDER_TIMEOUT_SECONDS = 1800
QUEUE_RECONCILE_PROBES = 3  # ioreg + sysctl in Queue construction, then ps for an active attempt
QUEUE_RECONCILE_PROBE_SECONDS = QUEUE_RECONCILE_PROBES * SUBPROCESS_PROBE_TIMEOUT
COORDINATOR_METADATA_KEYS = (
    "coordinator", "gc.coordinator", "gasvillage.coordinator", "gasvillage.reviewer",
)
ACTIVE_STATUSES = ("open", "in_progress", "blocked", "deferred")
OWNERSHIP_FILTERS_PER_RIG = len(COORDINATOR_METADATA_KEYS) + 1  # metadata aliases plus exact assignee
MAX_OWNERSHIP_QUERIES = MAX_CITY_RIGS * OWNERSHIP_FILTERS_PER_RIG * 2  # active and closed passes
MAX_REQUEST_QUERIES = 2  # active and closed lab-book request passes
# One config read, one order-history query, 240 server-filtered ownership queries
# (four metadata aliases plus exact assignee, active and closed across 24 rigs),
# two lab-book request queries, 24 linked-bead lookups, 12 root-scoped pause
# queries (metadata and parent paths), nine nudge recipients, and queue probes.
MAX_EXTERNAL_COMMAND_SECONDS = (
    CONFIG_TIMEOUT
    + STORE_TIMEOUT * (1 + MAX_OWNERSHIP_QUERIES + MAX_REQUEST_QUERIES
                       + MAX_GRAPH_LOOKUPS + 2 * MAX_TRACKED_ROOTS)
    + NUDGE_TIMEOUT * MAX_NUDGE_RECIPIENTS
    + QUEUE_RECONCILE_PROBE_SECONDS
)

BEAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LINK_METADATA_FIELDS = (
    ("gc.root_bead_id", "gc.root_store_ref"),
    ("gc.review_dispatch_root", "gc.review_dispatch_store_ref"),
    ("gc.independent_review_root", "gc.independent_review_store_ref"),
    ("gc.input_convoy_id", "gc.root_store_ref"),
    ("source_task", "source_store_ref"),
    ("gc.source_work_bead", "gc.source_store_ref"),
    ("delegated_work_phase", "delegated_work_store_ref"),
    ("replan_successor", "replan_store_ref"),
    ("predecessor_review", "review_dispatch_store_ref"),
    ("labtech.workflow_ref", None),
)
NON_CYCLE_BACKLINK_RELATIONS = frozenset({
    "gc.root_bead_id",
    "gc.input_convoy_id",
})
CURRENT_BRIDGE_RELATIONS = frozenset({
    "review",
    "labtech.workflow_ref",
    "gc.root_bead_id",
    "gc.input_convoy_id",
    "source_task",
    "gc.source_work_bead",
    "delegated_work_phase",
    "replan_successor",
})


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
            f"effective city has {len(rig_names)} rigs; coordinator scan is bounded at {MAX_CITY_RIGS}"
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
    # bd list --id returns the same identity/metadata/edge fields without the
    # unbounded description and notes returned by bd show.
    argv = [str(context["gc"]), "--city", str(context["city"]), "--rig", rig,
            "bd", "list", "--id", bead_id, "--all", "--brief",
            "--max-rows", "2", "--json"]
    value = command_json(argv, runner=runner, timeout=STORE_TIMEOUT,
                         label=f"linked bead lookup for {rig}/{bead_id}")
    candidates = rows(value)
    matches = [item for item in candidates if isinstance(item, dict) and item.get("id") == bead_id]
    if len(matches) == 1:
        return matches[0]
    raise WatchdogError(f"linked bead lookup for {rig}/{bead_id} returned {len(matches)} exact rows")


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
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in COORDINATOR_METADATA_KEYS:
        if metadata.get(key) == target:
            return key
    return None


def active_status(bead):
    return isinstance(bead, dict) and bead.get("status") in ACTIVE_STATUSES


def has_linked_work_evidence(bead):
    """Terminal rows are useful anchors only when they retain explicit workflow links."""
    metadata = bead.get("metadata") if isinstance(bead, dict) else None
    if isinstance(metadata, dict) and any(
        metadata.get(field_name) not in (None, "", False, "false", "0")
        for field_name, _store_field in LINK_METADATA_FIELDS
        if field_name != "gc.root_bead_id"
    ):
        return True
    dependencies = bead.get("dependencies")
    return isinstance(dependencies, list) and bool(dependencies)


def responsibility_seed_priority(bead):
    if active_status(bead):
        return 0
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    has_current_link = any(
        metadata.get(field_name) not in (None, "", False, "false", "0")
        for field_name, _store_field in LINK_METADATA_FIELDS
        if field_name not in ("gc.root_bead_id", "predecessor_review")
    )
    dependencies = bead.get("dependencies")
    return 1 if has_current_link or (isinstance(dependencies, list) and bool(dependencies)) else 2


def linked_graph_priority(bead, relation, source_priority):
    if active_status(bead):
        return 0
    if relation in CURRENT_BRIDGE_RELATIONS and source_priority < 2:
        return source_priority
    return 2


def rig_from_store_ref(context, current_rig, store_ref, component):
    if store_ref in (None, ""):
        return current_rig
    if not isinstance(store_ref, str):
        raise ValueError(f"{component} has a non-string store reference")
    rig = store_ref[4:] if store_ref.startswith("rig:") else store_ref
    if not RIG_NAME_RE.fullmatch(rig):
        raise ValueError(f"{component} has malformed store reference {store_ref!r}")
    if rig not in context["rig_names"]:
        raise ValueError(f"{component} points to unconfigured rig store {rig!r}")
    return rig


def parse_workflow_ref(context, value, component):
    if not isinstance(value, str) or not value.startswith("rig:") or "/" not in value[4:]:
        raise ValueError(f"{component} must use rig:<rig>/<bead-id>")
    rig, bead_id = value[4:].split("/", 1)
    if not RIG_NAME_RE.fullmatch(rig) or rig not in context["rig_names"]:
        raise ValueError(f"{component} names an invalid or unconfigured rig store {rig!r}")
    if not BEAD_ID_RE.fullmatch(bead_id):
        raise ValueError(f"{component} has malformed bead ID {bead_id!r}")
    return (rig, bead_id)


def linked_ref(context, rig, metadata, field_name, store_field):
    value = metadata.get(field_name)
    if value in (None, ""):
        return None, None
    component = f"{field_name} on {rig}"
    if field_name == "labtech.workflow_ref":
        try:
            return parse_workflow_ref(context, value, component), None
        except ValueError as exc:
            return None, str(exc)
    if not isinstance(value, str):
        return None, f"{component} is not a string bead ID"
    if value.startswith("rig:"):
        try:
            return parse_workflow_ref(context, value, component), None
        except ValueError as exc:
            return None, str(exc)
    if not BEAD_ID_RE.fullmatch(value):
        return None, f"{component} has malformed bead ID {value!r}"
    store_ref = metadata.get(store_field) if store_field else None
    if not store_ref:
        store_ref = metadata.get("gc.root_store_ref")
    try:
        return (rig_from_store_ref(context, rig, store_ref, component), value), None
    except ValueError as exc:
        return None, str(exc)


def tracking_error(findings, errors, rig, bead_id, message, component="responsibility-tracking"):
    errors.append({"component": component, "rig": rig, "bead_id": bead_id, "message": message})
    findings.append({"type": "tracking_error", "severity": "coordinator", "component": component,
                     "rig": rig, "bead_id": bead_id, "message": message})


def bead_refs(context, rig, bead):
    refs = []
    errors = []
    blockers = []
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    bead_id = bead.get("id")
    review_fields = ("gc.review_dispatch_root", "gc.independent_review_root")
    for field_name, store_field in LINK_METADATA_FIELDS:
        ref, error = linked_ref(context, rig, metadata, field_name, store_field)
        if error:
            errors.append(error)
        elif ref:
            relation = "review" if field_name in review_fields else field_name
            refs.append((ref, relation))

    parent = bead.get("parent") or bead.get("parent_id")
    if parent:
        parent_ref, error = linked_ref(context, rig, {"parent": parent}, "parent", None)
        if error:
            errors.append(error)
        elif parent_ref:
            refs.append((parent_ref, "parent"))

    dependencies = bead.get("dependencies") or []
    if not isinstance(dependencies, list):
        errors.append(f"{rig}/{bead_id} has an unreadable dependency list")
        dependencies = []
    if len(dependencies) > MAX_LINKED_DUTIES:
        errors.append(f"{rig}/{bead_id} has {len(dependencies)} dependency links; scan bound is {MAX_LINKED_DUTIES}")
    for edge in dependencies[:MAX_LINKED_DUTIES]:
        if not isinstance(edge, dict):
            errors.append(f"{rig}/{bead_id} has a malformed dependency link")
            continue
        dep_id = edge.get("id") or edge.get("depends_on_id") or edge.get("issue_id")
        dep_type = edge.get("dependency_type") or edge.get("type")
        if not dep_type:
            errors.append(f"{rig}/{bead_id} has a dependency without a relationship type")
            continue
        if not isinstance(dep_id, str) or not BEAD_ID_RE.fullmatch(dep_id):
            errors.append(f"{rig}/{bead_id} has a malformed {dep_type} dependency ID {dep_id!r}")
            continue
        raw_edge_metadata = edge.get("metadata")
        if raw_edge_metadata in (None, ""):
            edge_metadata = {}
        elif isinstance(raw_edge_metadata, dict):
            edge_metadata = raw_edge_metadata
        elif isinstance(raw_edge_metadata, str):
            try:
                edge_metadata = json.loads(raw_edge_metadata)
            except json.JSONDecodeError:
                errors.append(f"{rig}/{bead_id} has malformed JSON dependency metadata")
                continue
            if not isinstance(edge_metadata, dict):
                errors.append(f"{rig}/{bead_id} dependency metadata JSON is not an object")
                continue
        else:
            errors.append(f"{rig}/{bead_id} dependency metadata is not an object or JSON string")
            continue
        edge_store = edge.get("store_ref")
        metadata_store = edge_metadata.get("gc.edge_store_ref")
        try:
            edge_rig = (rig_from_store_ref(context, rig, edge_store,
                                           f"{dep_type} dependency from {rig}/{bead_id}")
                        if edge_store not in (None, "") else None)
            metadata_rig = (rig_from_store_ref(context, rig, metadata_store,
                                               f"{dep_type} dependency from {rig}/{bead_id}")
                            if metadata_store not in (None, "") else None)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if edge_rig and metadata_rig and edge_rig != metadata_rig:
            errors.append(f"{dep_type} dependency from {rig}/{bead_id} has conflicting store references")
            continue
        dep_rig = edge_rig or metadata_rig or rig
        refs.append(((dep_rig, dep_id), dep_type))
        dep_status = edge.get("status")
        if dep_type == "blocks" and dep_status and dep_status != "closed":
            blockers.append({
                "type": "blocked_prerequisite", "severity": "coordinator" if dep_status in ("blocked", "deferred") else "george",
                "rig": dep_rig, "bead_id": dep_id, "status": dep_status,
                "coordinator_bead_id": bead_id, "coordinator_rig": rig,
                "title": edge.get("title", ""),
                "message": "blocking prerequisite remains open on its original bead",
            })

    unique_refs = []
    seen = set()
    for ref, relation in refs:
        if ref not in seen:
            seen.add(ref)
            unique_refs.append((ref, relation))
    return unique_refs, errors, blockers


def active_responsibility_finding(rig, bead, target):
    status = bead.get("status")
    if status not in ("open", "in_progress", "blocked", "deferred"):
        return None
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    if metadata.get("gasvillage.pause_state") == "awaiting_review":
        return None
    explicit_pause = any(metadata.get(key) not in (None, "", False, "false", "0")
                         for key in ("dispatch_hold", "pause_requested", "coordinator_review_required"))
    if metadata.get("gasvillage.pause_state") == "awaiting_review" or status == "blocked":
        kind, severity = "blocked_responsibility", "coordinator"
    elif explicit_pause or status == "deferred":
        kind, severity = "explicit_pause", "coordinator"
    else:
        kind, severity = "coordinator_responsibility", "george"
    binding = coordinator_binding(metadata, target)
    direct_assignment = not binding and bead.get("assignee") == target
    return {
        "type": kind, "severity": severity, "rig": rig, "bead_id": bead.get("id"),
        "title": bead.get("title", ""), "status": status, "assignee": bead.get("assignee"),
        "coordinator": target if binding else None, "responsibility_owner": target,
        "responsibility_source": "direct_assignment" if direct_assignment else "coordinator_metadata",
        "coordinator_metadata_key": binding,
        "pause_state": metadata.get("gasvillage.pause_state"),
        "message": "durable responsibility remains tracked; coordinator binding and current executor are reported separately",
    }


def review_converged(bead):
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    return (
        bead.get("status") == "closed"
        and metadata.get("gc.review.loop_outcome") == "done"
        and metadata.get("gc.review.synthesis_verdict") == "pass"
        and metadata.get("gc.review.test_result") == "pass"
    )


def retired_review_outcome(bead):
    """Return whether a closed review records a replan/retirement outcome."""
    if not isinstance(bead, dict):
        return False
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    return bead.get("status") == "closed" and (
        metadata.get("gc.outcome") in ("skipped", "abandoned")
        or metadata.get("gc.review.loop_outcome") in ("replan", "skipped", "abandoned")
        or metadata.get("work_outcome") in ("abandoned", "skipped")
    )


def review_finding(rig, bead, source_ref):
    status = bead.get("status")
    if review_converged(bead):
        return None
    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    if status in ("open", "in_progress"):
        kind, severity = "active_independent_review", "george"
        message = "implementation completion does not discharge responsibility while its independent review is active"
    elif status in ("blocked", "deferred"):
        kind, severity = "blocked_review_workflow", "coordinator"
        message = "independent review workflow is blocked on its original bead"
    else:
        kind, severity = "review_not_converged", "coordinator"
        message = "closed review lacks recorded synthesis and test convergence evidence"
    return {
        "type": kind, "severity": severity, "rig": rig, "bead_id": bead.get("id"),
        "title": bead.get("title", ""), "status": status, "assignee": bead.get("assignee"),
        "source_workflow": f"rig:{source_ref[0]}/{source_ref[1]}",
        "review_loop_outcome": metadata.get("gc.review.loop_outcome"),
        "synthesis_verdict": metadata.get("gc.review.synthesis_verdict"),
        "test_result": metadata.get("gc.review.test_result"),
        "message": message,
    }


def add_pause_findings(context, root_ref, runner, findings, errors, seen_pauses):
    rig, root_id = root_ref
    queries = (
        ["--metadata-field", f"gc.root_bead_id={root_id}"],
        ["--parent", root_id],
    )
    for scope_args in queries:
        try:
            candidates = run_bd_list(context, rig, [
                "--status", "blocked", "--metadata-field", "gasvillage.pause_state=awaiting_review",
                *scope_args, "--brief", "--max-rows", str(MAX_PAUSE_CANDIDATES + 1), "--json",
            ], runner)
        except WatchdogError as exc:
            tracking_error(findings, errors, rig, root_id,
                           f"cannot inspect pauses linked to rig:{rig}/{root_id}: {exc}",
                           component="linked-pause-scan")
            continue
        if len(candidates) > MAX_PAUSE_CANDIDATES:
            tracking_error(findings, errors, rig, root_id,
                           f"linked pause rows exceeded the {MAX_PAUSE_CANDIDATES}-row bound; coverage is partial",
                           component="linked-pause-scan")
        for bead in candidates[:MAX_PAUSE_CANDIDATES]:
            if not isinstance(bead, dict) or bead.get("status") != "blocked":
                continue
            metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
            if metadata.get("gasvillage.pause_state") != "awaiting_review":
                continue
            pause_id = bead.get("id")
            if not isinstance(pause_id, str) or not BEAD_ID_RE.fullmatch(pause_id):
                tracking_error(findings, errors, rig, root_id,
                               "linked pause query returned a row without a valid original bead ID",
                               component="linked-pause-scan")
                continue
            key = (rig, pause_id)
            if key in seen_pauses:
                continue
            seen_pauses.add(key)
            findings.append({
                "type": "blocked_review", "severity": "coordinator", "rig": rig,
                "bead_id": pause_id, "title": bead.get("title", ""), "status": "blocked",
                "assignee": bead.get("assignee"), "root_bead_id": root_id,
                "pause_state": metadata.get("gasvillage.pause_state"),
                "review_condition": metadata.get("gasvillage.review_condition"),
                "message": "original blocked pause is linked to a configured coordinator workflow",
            })


def coordinator_responsibility_findings(context, target, runner):
    findings = []
    errors = []
    seeds = {}
    request_links = {}
    ownership_filters = [
        (f"metadata {key}", ["--metadata-field", f"{key}={target}"], key)
        for key in COORDINATOR_METADATA_KEYS
    ]
    ownership_filters.append(("direct assignee", ["--assignee", target], None))

    def collect_owner_pass(status_filter, expected_statuses, limit, component):
        candidates_by_key = {}
        for rig in context["rig_names"]:
            for filter_label, filter_args, metadata_key in ownership_filters:
                try:
                    candidates = run_bd_list(context, rig, [
                        *filter_args, "--status", status_filter, "--brief",
                        "--max-rows", str(limit), "--json",
                    ], runner)
                except WatchdogError as exc:
                    tracking_error(findings, errors, rig, None,
                                   f"cannot inspect server-filtered {status_filter} responsibility rows for {filter_label}: {exc}; coverage is partial",
                                   component=component)
                    continue
                if len(candidates) >= limit:
                    tracking_error(findings, errors, rig, None,
                                   f"{status_filter} {filter_label} responsibility query reached its {limit}-row bound; returned rows may be truncated and coverage is partial",
                                   component=component)
                for bead in candidates:
                    if not isinstance(bead, dict):
                        tracking_error(findings, errors, rig, None,
                                       f"{status_filter} {filter_label} query returned a malformed bead row",
                                       component=component)
                        continue
                    metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
                    if metadata_key is not None:
                        if metadata.get(metadata_key) != target:
                            tracking_error(findings, errors, rig, bead.get("id"),
                                           f"server-filtered {filter_label} query returned a row without the exact configured binding",
                                           component=component)
                            continue
                        role = "coordinator"
                    else:
                        if bead.get("assignee") != target:
                            tracking_error(findings, errors, rig, bead.get("id"),
                                           "server-filtered assignee query returned a row without the exact configured target assignment",
                                           component=component)
                            continue
                        role = "coordinator" if coordinator_binding(metadata, target) else "assignee"
                    if bead.get("status") not in expected_statuses:
                        tracking_error(findings, errors, rig, bead.get("id"),
                                       f"server-filtered {status_filter} query returned a row in status {bead.get('status')!r}",
                                       component=component)
                        continue
                    bead_id = bead.get("id")
                    if not isinstance(bead_id, str) or not BEAD_ID_RE.fullmatch(bead_id):
                        tracking_error(findings, errors, rig, None,
                                       f"{status_filter} responsibility query returned a malformed bead ID",
                                       component=component)
                        continue
                    key = (rig, bead_id)
                    previous = candidates_by_key.get(key)
                    if previous is None or (previous[1] != "coordinator" and role == "coordinator"):
                        candidates_by_key[key] = (bead, role)
        return candidates_by_key

    active_owners = collect_owner_pass(
        ",".join(ACTIVE_STATUSES), set(ACTIVE_STATUSES), MAX_OWNED_ROWS + 1,
        "responsibility-seed-scan",
    )
    if len(active_owners) > MAX_OWNED_ROWS:
        tracking_error(findings, errors, context["rig"], None,
                       f"active coordinator responsibility scan exceeded its {MAX_OWNED_ROWS}-row bound; coverage is partial",
                       component="responsibility-seed-scan")
    active_status_order = {"blocked": 0, "in_progress": 1, "open": 2, "deferred": 3}
    active_owner_items = sorted(
        active_owners.items(),
        key=lambda item: (active_status_order.get(item[1][0].get("status"), 4), item[0]),
    )[:MAX_OWNED_ROWS]
    for key, (bead, role) in active_owner_items:
        seeds[key] = (bead, role)
        duty = active_responsibility_finding(key[0], bead, target)
        if duty:
            findings.append(duty)

    closed_owners = collect_owner_pass("closed", {"closed"}, MAX_CLOSED_OWNED_ROWS + 1,
                                       "closed-responsibility-scan")
    linked_closed_owners = [
        (key, value) for key, value in closed_owners.items() if has_linked_work_evidence(value[0])
    ]
    if len(linked_closed_owners) > MAX_CLOSED_OWNED_ROWS:
        tracking_error(findings, errors, context["rig"], None,
                       f"closed workflow-anchor scan exceeded its {MAX_CLOSED_OWNED_ROWS}-row bound; coverage is partial",
                       component="closed-responsibility-scan")

    def closed_anchor_order(item):
        key, (bead, _role) = item
        metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
        has_review_link = any(metadata.get(name) not in (None, "", False, "false", "0")
                              for name in ("gc.review_dispatch_root", "gc.independent_review_root"))
        return (0 if has_review_link else 1, key)

    for key, value in sorted(linked_closed_owners, key=closed_anchor_order)[:MAX_CLOSED_OWNED_ROWS]:
        seeds.setdefault(key, value)

    def request_pass(status_filter, expected_statuses, limit, include_closed):
        try:
            request_rows = run_bd_list(context, context["rig"], [
                "--parent", context["lab_book"], "--label", "labtech-request",
                "--status", status_filter, "--brief", "--max-rows", str(limit), "--json",
            ], runner)
        except WatchdogError as exc:
            tracking_error(findings, errors, context["rig"], context["lab_book"],
                           f"cannot inspect {status_filter} requests in the configured lab book: {exc}; coverage is partial",
                           component="request-scan")
            return
        if len(request_rows) > limit - 1:
            tracking_error(findings, errors, context["rig"], context["lab_book"],
                           f"{status_filter} lab-book request scan reached its {limit - 1}-row bound; coverage is partial",
                           component="request-scan")
        rows_by_key = {}
        for bead in request_rows[:limit - 1]:
            if not isinstance(bead, dict):
                tracking_error(findings, errors, context["rig"], context["lab_book"],
                               "lab-book request query returned a malformed row", component="request-scan")
                continue
            bead_id = bead.get("id")
            labels = bead.get("labels") if isinstance(bead.get("labels"), list) else []
            if bead.get("parent") != context["lab_book"] or "labtech-request" not in labels:
                tracking_error(findings, errors, context["rig"], bead_id,
                               "lab-book request query returned a bead outside its configured parent/label scope",
                               component="request-scan")
                continue
            if not isinstance(bead_id, str) or not BEAD_ID_RE.fullmatch(bead_id):
                tracking_error(findings, errors, context["rig"], None,
                               "lab-book request query returned a malformed bead ID", component="request-scan")
                continue
            if bead.get("status") not in expected_statuses:
                tracking_error(findings, errors, context["rig"], bead_id,
                               f"server-filtered {status_filter} request query returned status {bead.get('status')!r}",
                               component="request-scan")
                continue
            key = (context["rig"], bead_id)
            metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
            raw_link = metadata.get("labtech.workflow_ref")
            if raw_link:
                ref, error = linked_ref(context, context["rig"], metadata, "labtech.workflow_ref", None)
                if error:
                    tracking_error(findings, errors, context["rig"], bead_id, error, component="request-link")
                elif ref:
                    request_links[key] = ref
            elif not include_closed:
                tracking_error(findings, errors, context["rig"], bead_id,
                               "active labtech-request bead has no structured labtech.workflow_ref; preparation/workflow coverage is unresolved",
                               component="request-link")
            if not include_closed or has_linked_work_evidence(bead):
                rows_by_key[key] = (bead, "request")
            if not include_closed:
                findings.append({
                    "type": "labtech_request",
                    "severity": "coordinator" if bead.get("status") in ("blocked", "deferred") else "george",
                    "rig": context["rig"], "bead_id": bead_id, "title": bead.get("title", ""),
                    "status": bead.get("status"), "workflow_ref": raw_link,
                    "message": "configured lab-book request is tracked before executable queue submission",
                })
        if include_closed and len(rows_by_key) > MAX_CLOSED_REQUEST_ROWS:
            tracking_error(findings, errors, context["rig"], context["lab_book"],
                           f"closed linked lab-book request scan exceeded its {MAX_CLOSED_REQUEST_ROWS}-row bound; coverage is partial",
                           component="request-scan")
        request_items = list(rows_by_key.items())
        if include_closed:
            def request_anchor_order(item):
                _key, (bead, _role) = item
                metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
                review_link = any(metadata.get(name) not in (None, "", False, "false", "0")
                                  for name in ("gc.review_dispatch_root", "gc.independent_review_root"))
                return (0 if review_link else 1, item[0])
            request_items.sort(key=request_anchor_order)
        for key, value in request_items[:MAX_CLOSED_REQUEST_ROWS if include_closed else MAX_REQUEST_ROWS]:
            seeds.setdefault(key, value)

    request_pass(",".join(ACTIVE_STATUSES), set(ACTIVE_STATUSES), MAX_REQUEST_ROWS + 1, False)
    request_pass("closed", {"closed"}, MAX_CLOSED_REQUEST_ROWS + 1, True)

    if len(seeds) > MAX_GRAPH_NODES:
        tracking_error(findings, errors, context["rig"], None,
                       f"coordinator plus request seed rows exceeded the {MAX_GRAPH_NODES}-node graph bound; coverage is partial")

    pending_by_priority = [deque(), deque(), deque()]
    queued_priority = {}
    graph_nodes = {}
    visited = set()
    review_refs = set()
    review_sources = {}
    review_source_priorities = {}
    review_source_anchors = {}
    review_successor_edges = []
    review_predecessor_edges = []
    pause_roots = {}
    blocking_refs = set()
    blocking_origins = {}
    blocking_edges = set()
    membership_edges = set()
    input_convoy_refs = set()
    attempted_refs = set()
    graph_lookups = 0

    def mark_pause_root(ref, priority):
        previous = pause_roots.get(ref)
        if previous is None or priority < previous:
            pause_roots[ref] = priority

    def enqueue(ref, linked_bead, depth, role, path, priority):
        if ref in visited:
            return
        previous = queued_priority.get(ref)
        if previous is not None and previous <= priority:
            return
        queued_priority[ref] = priority
        item = (ref, linked_bead, depth, role, path, priority)
        pending_by_priority[priority].append(item)

    for key, (bead, role) in seeds.items():
        priority = responsibility_seed_priority(bead)
        enqueue(key, bead, 0, role, frozenset({key}), priority)

    while any(pending_by_priority):
        pending = next(queue for queue in pending_by_priority if queue)
        key, bead, depth, role, path, priority = pending.popleft()
        if queued_priority.get(key) != priority:
            continue
        queued_priority.pop(key, None)
        if key in visited:
            continue
        if len(visited) >= MAX_GRAPH_NODES:
            tracking_error(findings, errors, key[0], key[1],
                           f"linked responsibility graph exceeded the {MAX_GRAPH_NODES}-node bound; coverage is partial")
            break
        visited.add(key)
        graph_nodes[key] = bead
        if bead.get("metadata") not in (None, {}) and not isinstance(bead.get("metadata"), dict):
            tracking_error(findings, errors, key[0], key[1],
                           "linked bead metadata has an unreadable non-object shape")
        metadata = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
        if metadata.get("gasvillage.pause_state") == "awaiting_review" and bead.get("status") == "blocked":
            findings.append({
                "type": "blocked_review", "severity": "coordinator", "rig": key[0],
                "bead_id": key[1], "title": bead.get("title", ""), "status": "blocked",
                "assignee": bead.get("assignee"), "pause_state": "awaiting_review",
                "message": "original blocked pause is linked through the coordinator responsibility graph",
            })
        if bead.get("status") in ("blocked", "deferred") and role not in ("coordinator", "request"):
            findings.append({
                "type": "blocked_responsibility", "severity": "coordinator", "rig": key[0],
                "bead_id": key[1], "title": bead.get("title", ""), "status": bead.get("status"),
                "assignee": bead.get("assignee"),
                "message": "linked workflow prerequisite remains blocked on its original bead",
            })
        owner_key = coordinator_binding(metadata, target)
        if owner_key and role not in ("coordinator", "request"):
            duty = active_responsibility_finding(key[0], bead, target)
            if duty:
                findings.append(duty)

        root_value = metadata.get("gc.root_bead_id")
        if root_value:
            ref, error = linked_ref(context, key[0], metadata, "gc.root_bead_id", "gc.root_store_ref")
            if error:
                tracking_error(findings, errors, key[0], key[1], error)
            elif ref:
                mark_pause_root(ref, priority)
        elif role in ("coordinator", "assignee") and active_status(bead):
            mark_pause_root(key, priority)
        elif role == "request" and key in request_links:
            mark_pause_root(request_links[key], priority)
        elif role == "labtech.workflow_ref" or (
            role == "source_task" and active_status(bead)
        ):
            mark_pause_root(key, priority)

        refs, ref_errors, blockers = bead_refs(context, key[0], bead)
        for error in ref_errors:
            tracking_error(findings, errors, key[0], key[1], error)
        findings.extend(blockers)
        for ref, relation in refs:
            if relation == "blocks":
                blocking_refs.add(ref)
                blocking_origins.setdefault(ref, key)
                blocking_edges.add((key, ref))
            if relation == "tracks":
                membership_edges.add((key, ref))
            if relation == "gc.input_convoy_id":
                input_convoy_refs.add(ref)
            if relation == "review":
                previous_priority = review_source_priorities.get(ref)
                if previous_priority is None or priority < previous_priority:
                    review_source_priorities[ref] = priority
                    review_sources[ref] = key
                review_source_anchors.setdefault(ref, set()).add(key)
            if relation == "replan_successor":
                review_successor_edges.append((key, ref))
            if relation == "predecessor_review":
                review_predecessor_edges.append((key, ref))
            if ref in path:
                native_backlink = relation in NON_CYCLE_BACKLINK_RELATIONS and ref != key
                # An explicitly referenced input convoy tracks its source as
                # membership, regardless of whether the convoy is still open.
                native_backlink = native_backlink or (
                    relation == "tracks" and ref != key
                    and bead.get("issue_type") == "convoy" and key in input_convoy_refs
                )
                native_backlink = native_backlink or (
                    relation in ("source_task", "gc.source_work_bead")
                    and ref != key and bead.get("status") == "closed"
                    and (ref, key) in blocking_edges
                )
                native_backlink = native_backlink or (
                    relation == "parent" and ref != key and bead.get("status") == "closed"
                    and ((ref, key) in membership_edges or (ref, key) in blocking_edges)
                )
                if native_backlink:
                    # Native owner, source, parent, convoy, and closed-phase
                    # membership back-links retain provenance without forming
                    # another executable workflow edge. Other semantic loops
                    # still produce the cycle diagnostic below.
                    continue
                tracking_error(findings, errors, key[0], key[1],
                               f"cycle detected in explicit responsibility link to {ref[0]}/{ref[1]}; the repeated node was deduplicated",
                               component="linked-bead")
                continue
            if ref in visited:
                continue
            if depth >= MAX_GRAPH_DEPTH:
                tracking_error(findings, errors, key[0], key[1],
                               f"linked responsibility graph reached the depth {MAX_GRAPH_DEPTH} bound at {ref[0]}/{ref[1]}")
                continue
            if ref in seeds:
                linked_bead, linked_role = seeds[ref]
                linked_priority = linked_graph_priority(linked_bead, relation, priority)
                enqueue(ref, linked_bead, depth + 1, linked_role, path | {ref}, linked_priority)
                continue
            if ref in attempted_refs:
                continue
            if graph_lookups >= MAX_GRAPH_LOOKUPS:
                tracking_error(findings, errors, key[0], key[1],
                               f"linked bead reads exceeded the {MAX_GRAPH_LOOKUPS}-lookup bound; coverage is partial")
                continue
            graph_lookups += 1
            attempted_refs.add(ref)
            try:
                linked_bead = run_bd_show(context, ref[0], ref[1], runner)
            except WatchdogError as exc:
                tracking_error(findings, errors, ref[0], ref[1],
                               f"cannot read explicit {relation} link from {key[0]}/{key[1]}: {exc}",
                               component="linked-bead")
                continue
            linked_priority = linked_graph_priority(linked_bead, relation, priority)
            enqueue(ref, linked_bead, depth + 1, relation, path | {ref}, linked_priority)

    superseded_review_refs = set()
    for predecessor, successor in review_successor_edges:
        source_priority = review_source_priorities.get(predecessor)
        review_bead = graph_nodes.get(predecessor)
        if (source_priority is None or successor not in graph_nodes
                or not retired_review_outcome(review_bead)):
            continue
        superseded_review_refs.add(predecessor)

    # A predecessor link is explicit history only when its anchor also
    # identifies a current review root. A predecessor reference alone must not
    # retire an otherwise-current review root.
    current_review_anchors = set(review_source_priorities)
    for anchors in review_source_anchors.values():
        current_review_anchors.update(anchors)
    for anchor, predecessor in review_predecessor_edges:
        if anchor in current_review_anchors:
            superseded_review_refs.add(predecessor)

    for ref, source_priority in review_source_priorities.items():
        review_bead = graph_nodes.get(ref)
        if review_bead is None:
            if source_priority < 2:
                tracking_error(findings, errors, ref[0], ref[1],
                               "explicit review-dispatch root was not readable in the tracked graph",
                               component="review-convergence")
            continue
        if ref in superseded_review_refs:
            continue
        if source_priority >= 2 and not active_status(review_bead):
            continue
        review_refs.add(ref)
        current_priority = 0 if active_status(review_bead) else min(source_priority, 1)
        mark_pause_root(ref, current_priority)

    for ref in sorted(blocking_refs):
        prerequisite = graph_nodes.get(ref)
        if prerequisite is None or prerequisite.get("status") == "closed":
            continue
        origin = blocking_origins.get(ref)
        findings.append({
            "type": "blocked_prerequisite",
            "severity": "coordinator" if prerequisite.get("status") in ("blocked", "deferred") else "george",
            "rig": ref[0], "bead_id": ref[1], "title": prerequisite.get("title", ""),
            "status": prerequisite.get("status"),
            "coordinator_bead_id": origin[1] if origin else None,
            "coordinator_rig": origin[0] if origin else None,
            "message": "blocking prerequisite remains open on its original bead",
        })

    seen_pauses = set()
    seen_root_refs = set()
    for root_ref, root_priority in sorted(pause_roots.items(), key=lambda item: (item[1], item[0])):
        if root_ref in seen_root_refs:
            continue
        if len(seen_root_refs) >= MAX_TRACKED_ROOTS:
            current_root_count = sum(priority < 2 for priority in pause_roots.values())
            if current_root_count > MAX_TRACKED_ROOTS:
                message = (f"current linked workflow root scan exceeded the {MAX_TRACKED_ROOTS}-root bound; "
                           "current pause coverage is partial")
            else:
                message = (f"linked workflow root scan exceeded the {MAX_TRACKED_ROOTS}-root bound after "
                           "current roots; retired-history pause coverage is partial")
            tracking_error(findings, errors, root_ref[0], root_ref[1],
                           message,
                           component="linked-pause-scan")
            break
        seen_root_refs.add(root_ref)
        add_pause_findings(context, root_ref, runner, findings, errors, seen_pauses)

    for ref in sorted(review_refs):
        review_bead = graph_nodes.get(ref)
        if review_bead is None:
            continue
        source_ref = review_sources.get(ref, ref)
        finding = review_finding(ref[0], review_bead, source_ref)
        if finding:
            findings.append(finding)

    return findings, errors


def receipt_findings(queue, checkpoint):
    findings = []
    errors = []
    active = checkpoint.get("active") if isinstance(checkpoint, dict) else None
    active_id = active.get("attempt_id") if isinstance(active, dict) else None
    attempts_dir = queue.root / "attempts"
    if not attempts_dir.exists():
        return findings, errors
    folders = list(islice(attempts_dir.iterdir(), MAX_ATTEMPT_ROWS + 1))
    if len(folders) > MAX_ATTEMPT_ROWS:
        message = f"attempt receipt scan exceeded the {MAX_ATTEMPT_ROWS}-row bound; coverage is partial"
        errors.append({"component": "receipt-scan", "message": message})
        findings.append({"type": "tracking_error", "severity": "coordinator",
                         "component": "receipt-scan", "rig": queue.config.get("rig"),
                         "message": message})
    folders = folders[:MAX_ATTEMPT_ROWS]
    if active_id is not None:
        try:
            active_path = attempts_dir / label(active_id)
        except ValueError:
            active_path = None
        if active_path is not None and active_path.is_dir() and active_path not in folders:
            if len(folders) == MAX_ATTEMPT_ROWS:
                folders[-1] = active_path
            else:
                folders.append(active_path)
    for folder in folders:
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
    return findings, errors


def unique_findings(items):
    result = {}
    for item in items:
        if not item.get("bead_id") and item.get("type") in ("stale_reconciliation", "blocked_review"):
            item = {**item, "message": item.get("message", "") + "; bead ID missing in query result"}
        key = finding_key(item)
        result.setdefault(key, {**item, "key": key})
    if len(result) > MAX_FINDINGS:
        count = len(result)
        retained = dict(list(result.items())[:MAX_FINDINGS - 1])
        summary = {
            "type": "tracking_error", "severity": "coordinator", "component": "finding-limit",
            "rig": "city", "bead_id": "finding-limit",
            "message": f"observer found {count} distinct findings; report is capped at {MAX_FINDINGS} with partial coverage",
        }
        key = finding_key(summary)
        retained[key] = {**summary, "key": key}
        result = retained
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
    new_findings, review_errors = coordinator_responsibility_findings(context, context["target"], runner)
    findings.extend(new_findings)
    errors.extend(review_errors)
    new_findings, receipt_errors = receipt_findings(queue, checkpoint)
    findings.extend(new_findings)
    errors.extend(receipt_errors)

    if not context["coordinators"] and any(item.get("severity") == "coordinator" for item in findings):
        message = "queue config has no reviewer other than the George target; coordinator escalation is unavailable"
        errors.append({"component": "reviewers", "message": message})
        findings.append({"type": "observer_error", "severity": "george", "component": "reviewers",
                         "rig": context["rig"], "message": message})
    current = unique_findings(findings)
    if any(item.get("component") == "finding-limit" for item in current.values()):
        errors.append({"component": "finding-limit",
                       "message": "observer finding report was capped; inspect retained partial-coverage finding"})
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
