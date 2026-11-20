import json
import os
import re
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import reconcile_watchdog as watchdog


class FakeQueue:
    def __init__(self, home, checkpoint=None):
        self.root = Path(home) / "queue"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "attempts").mkdir(exist_ok=True)
        self.config = {
            "city": str((Path(home).parent / "city config with spaces").resolve()),
            "rig": "experiments",
        }
        self.checkpoint = checkpoint or {"active": None, "requests": [], "attempts": []}
        self.reconcile_calls = 0
        self.run_next_calls = 0

    def reconcile(self):
        self.reconcile_calls += 1
        return self.checkpoint

    def run_next(self):
        self.run_next_calls += 1
        raise AssertionError("watchdog must never launch an experiment")


class Fixture:
    def __init__(self, now=None, checkpoint=None):
        self.temp = tempfile.TemporaryDirectory(prefix="labtech watchdog with spaces ")
        self.root = Path(self.temp.name)
        self.city = self.root / "city config with spaces"
        self.home = self.root / "queue home with spaces"
        self.pack = self.root / "installed labtech pack with spaces"
        (self.city / "math-experiments").mkdir(parents=True)
        (self.home / "queue").mkdir(parents=True)
        (self.pack / "bin").mkdir(parents=True)
        (self.pack / "docs").mkdir(parents=True)
        (self.pack / "bin" / "serial_queue.py").write_text("# fixture\n")
        (self.pack / "docs" / "serial-queue.md").write_text("fixture\n")
        self.gc = Path("/private/fake gc")
        self.target = "experiments/labtech.george"
        self.now = now or datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
        (self.home / "queue" / "config.json").write_text(json.dumps({
            "city": str(self.city.resolve()), "rig": "experiments", "target": self.target,
            "lab_book": "mx-book", "gc": str(self.gc),
            "reviewers": [self.target, "gascity.mayor"],
        }))
        self.effective_config = {
            "ok": True,
            "city_path": str(self.city.resolve()),
            "config": {"Rigs": [
                {"Name": "experiments", "Path": str(self.city / "math-experiments"),
                 "FormulaVars": {"labtech_home": str(self.home), "labtech_pack": str(self.pack),
                                 "lab_book": "mx-book"}},
                {"Name": "DeRham", "Path": str(self.root / "derham"), "FormulaVars": {}},
                {"Name": "NewtonExperiments", "Path": str(self.root / "newton"), "FormulaVars": {}},
            ]},
        }
        self.stale_root = {
            "id": "old-reconcile-root", "title": "labtech-reconcile", "status": "open",
            "assignee": None, "created_at": watchdog.stamp(self.now - timedelta(hours=48)),
        }
        self.blocked_review = {
            "id": "blocked-review", "title": "Self-review pending coordinator disposition",
            "status": "blocked", "assignee": "NewtonExperiments/worker",
            "parent": None,
            "created_at": watchdog.stamp(self.now - timedelta(days=7)),
            "updated_at": watchdog.stamp(self.now - timedelta(hours=1)),
            "metadata": {"gasvillage.pause_state": "awaiting_review",
                         "gc.root_bead_id": "coordinator-root",
                         "gc.root_store_ref": "rig:NewtonExperiments",
                         "gasvillage.review_condition": "scope_clarified"},
        }
        self.coordinator_root = {
            "id": "coordinator-root", "title": "Coordinator responsibility",
            "status": "in_progress", "assignee": "NewtonExperiments/worker", "parent": None,
            "metadata": {"coordinator": self.target}, "dependencies": [],
        }
        self.owned_by_rig = {"NewtonExperiments": [self.coordinator_root]}
        self.request_rows = []
        self.linked_beads = {}
        self.unrelated_pauses = []
        self.queue = FakeQueue(self.home, checkpoint=checkpoint)
        self.calls = []
        self.fail_rig = None
        self.fail_nudge = set()
        self.queue_nudge = set()

    def close(self):
        self.temp.cleanup()

    def env(self):
        return {"GC_CITY_PATH": str(self.city), "GC_RIG": "experiments", "PACK_DIR": str(self.pack)}

    def runner(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if argv[0] == "/private/fake gc":
            pass
        if "config" in argv and "show" in argv:
            return CompletedProcess(argv, 0, json.dumps(self.effective_config), "")
        if "bd" in argv and "list" in argv:
            rig = argv[argv.index("--rig") + 1]
            if rig == self.fail_rig:
                return CompletedProcess(argv, 2, "", "store unavailable")
            if "--label" in argv:
                if "order-run:labtech-reconcile:rig:experiments" in argv:
                    rows = [self.stale_root]
                    if "--status" in argv:
                        statuses = set(argv[argv.index("--status") + 1].split(","))
                        rows = [row for row in rows if row.get("status") in statuses]
                    return CompletedProcess(argv, 0, json.dumps(rows), "")
                if "labtech-request" in argv:
                    rows = self.request_rows
                    if "--status" in argv:
                        statuses = set(argv[argv.index("--status") + 1].split(","))
                        rows = [row for row in rows if row.get("status") in statuses]
                    return CompletedProcess(argv, 0, json.dumps(rows), "")
            if "--id" in argv:
                bead_id = argv[argv.index("--id") + 1]
                matches = [bead for (stored_rig, _stored_id), bead in self.linked_beads.items()
                           if stored_rig == rig and bead.get("id") == bead_id]
                matches.extend(bead for rows in self.owned_by_rig.values()
                               for bead in rows if bead.get("id") == bead_id)
                matches.extend(bead for bead in self.request_rows if bead.get("id") == bead_id)
                return CompletedProcess(argv, 0, json.dumps(matches[:1]), "")
            if "--metadata-field" in argv:
                fields = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--metadata-field"]
                ownership_filter = next(((name, value.split("=", 1)[1])
                                         for name in watchdog.COORDINATOR_METADATA_KEYS
                                         for value in fields if value.startswith(name + "=")), None)
                if ownership_filter is not None:
                    name, target = ownership_filter
                    rows = [bead for bead in self.owned_by_rig.get(rig, [])
                            if (bead.get("metadata") or {}).get(name) == target]
                    if "--status" in argv:
                        statuses = set(argv[argv.index("--status") + 1].split(","))
                        rows = [bead for bead in rows if bead.get("status") in statuses]
                    return CompletedProcess(argv, 0, json.dumps(rows), "")
                if "gasvillage.pause_state=awaiting_review" in fields:
                    root_filter = next((value.split("=", 1)[1] for value in fields
                                        if value.startswith("gc.root_bead_id=")), None)
                    if root_filter is not None:
                        rows = [bead for bead in self.unrelated_pauses + [self.blocked_review]
                                if (bead.get("metadata") or {}).get("gc.root_bead_id") == root_filter]
                        return CompletedProcess(argv, 0, json.dumps(rows), "")
                    parent = argv[argv.index("--parent") + 1] if "--parent" in argv else None
                    if parent is not None:
                        rows = [bead for bead in self.unrelated_pauses + [self.blocked_review]
                                if bead.get("parent") == parent]
                        return CompletedProcess(argv, 0, json.dumps(rows), "")
                    return CompletedProcess(argv, 0, json.dumps(self.unrelated_pauses), "")
            if "--assignee" in argv:
                target = argv[argv.index("--assignee") + 1]
                rows = [bead for bead in self.owned_by_rig.get(rig, []) if bead.get("assignee") == target]
                if "--status" in argv:
                    statuses = set(argv[argv.index("--status") + 1].split(","))
                    rows = [bead for bead in rows if bead.get("status") in statuses]
                return CompletedProcess(argv, 0, json.dumps(rows), "")
            if "--parent" in argv and "--label" not in argv:
                parent = argv[argv.index("--parent") + 1]
                rows = [bead for bead in self.unrelated_pauses + [self.blocked_review]
                        if bead.get("parent") == parent]
                return CompletedProcess(argv, 0, json.dumps(rows), "")
            return CompletedProcess(argv, 0, "[]", "")
        if "session" in argv and "nudge" in argv:
            target = argv[argv.index("nudge") + 1]
            if target in self.fail_nudge:
                return CompletedProcess(argv, 1, "", "session nudge unavailable")
            if target in self.queue_nudge:
                result = {"ok": True, "queued": True, "outcome": "queued",
                          "delivery_attempted": False}
            else:
                result = {"ok": True, "queued": False, "outcome": "delivered",
                          "delivery_attempted": True}
            return CompletedProcess(argv, 0, json.dumps(result), "")
        raise AssertionError(f"unexpected command: {argv}")


class ReconcileWatchdogTests(unittest.TestCase):
    def test_order_is_twelve_hour_exec_with_no_work_gate_and_formula_is_preserved(self):
        root = Path(__file__).resolve().parents[1]
        order = (root / "orders" / "labtech-reconcile.toml").read_text()
        formula = (root / "formulas" / "labtech-reconcile.toml").read_text()
        self.assertRegex(order, r'(?m)^trigger\s*=\s*"cooldown"$')
        self.assertRegex(order, r'(?m)^interval\s*=\s*"12h"$')
        self.assertRegex(order, r'(?m)^timeout\s*=\s*"1800s"$')
        self.assertIn('exec = \'python3 "$PACK_DIR/bin/reconcile_watchdog.py"\'', order)
        self.assertRegex(order, r'(?m)^no_work_gate\s*=\s*true$')
        self.assertNotRegex(order, r'(?m)^formula\s*=')
        self.assertIn("12-hour", formula)
        self.assertGreater(watchdog.NUDGE_TIMEOUT, 36.17)
        self.assertEqual(watchdog.NUDGE_TIMEOUT, 60)
        self.assertEqual(watchdog.QUEUE_RECONCILE_PROBE_SECONDS, 90)
        self.assertEqual(watchdog.MAX_NUDGE_RECIPIENTS, 9)
        self.assertEqual(watchdog.OWNERSHIP_FILTERS_PER_RIG, 5)
        self.assertEqual(watchdog.MAX_OWNERSHIP_QUERIES, 240)
        self.assertEqual(watchdog.MAX_REQUEST_QUERIES, 2)
        self.assertEqual(watchdog.ORDER_TIMEOUT_SECONDS, 1800)
        self.assertEqual(watchdog.MAX_EXTERNAL_COMMAND_SECONDS, 1518)
        self.assertGreaterEqual(watchdog.ORDER_TIMEOUT_SECONDS - watchdog.MAX_EXTERNAL_COMMAND_SECONDS, 240)
        self.assertLessEqual(watchdog.MAX_EXTERNAL_COMMAND_SECONDS,
                             watchdog.ORDER_TIMEOUT_SECONDS)

    def test_legacy_gc_coordinator_alias_is_a_server_filtered_seed(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"] = {"gc.coordinator": fixture.target}
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            duty = next(item for item in result["findings"]
                        if item.get("bead_id") == "coordinator-root"
                        and item["type"] == "coordinator_responsibility")
            self.assertEqual(duty["coordinator_metadata_key"], "gc.coordinator")
            self.assertTrue(any("gc.coordinator=" + fixture.target in argv
                                for argv, _timeout in fixture.calls))
        finally:
            fixture.close()

    def test_direct_assignee_is_a_seed_and_reported_separately_from_coordinator_metadata(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"] = {}
        fixture.coordinator_root["assignee"] = fixture.target
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            duty = next(item for item in result["findings"]
                        if item.get("bead_id") == "coordinator-root"
                        and item["type"] == "coordinator_responsibility")
            self.assertIsNone(duty["coordinator"])
            self.assertEqual(duty["responsibility_owner"], fixture.target)
            self.assertEqual(duty["responsibility_source"], "direct_assignment")
            self.assertEqual(duty["assignee"], fixture.target)
            self.assertTrue(any("--assignee" in argv and
                                argv[argv.index("--assignee") + 1] == fixture.target
                                for argv, _timeout in fixture.calls))
            self.assertTrue(any(item["type"] == "blocked_review"
                                and item["bead_id"] == "blocked-review"
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_seed_scan_uses_bounded_native_filters_for_each_alias_and_status_tier(self):
        fixture = Fixture()
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                watchdog.run_observer(fixture.env(), fixture.runner,
                                      lambda _home: fixture.queue, fixture.now)
            seed_queries = [argv for argv, _timeout in fixture.calls
                            if ("--assignee" in argv or
                                any(argv[index + 1].startswith(
                                        tuple(key + "=" for key in watchdog.COORDINATOR_METADATA_KEYS))
                                    for index, value in enumerate(argv[:-1])
                                    if value == "--metadata-field"))]
            self.assertEqual(len(seed_queries), len(fixture.effective_config["config"]["Rigs"])
                             * watchdog.OWNERSHIP_FILTERS_PER_RIG * 2)
            self.assertEqual({argv[argv.index("--status") + 1] for argv in seed_queries},
                             {",".join(watchdog.ACTIVE_STATUSES), "closed"})
            for argv in seed_queries:
                self.assertIn("--brief", argv)
                self.assertIn("--max-rows", argv)
                self.assertIn("--json", argv)
        finally:
            fixture.close()

    def _install_closed_history_graph(self, fixture, historical_count, active_owner=True):
        historical = []
        for index in range(historical_count):
            root_id = f"historic-root-{index:02}"
            historical.append({
                "id": f"historic-owner-{index:02}", "status": "closed",
                "metadata": {"coordinator": fixture.target, "gc.root_bead_id": root_id},
                "dependencies": [],
            })
            fixture.linked_beads[("NewtonExperiments", root_id)] = {
                "id": root_id, "status": "closed", "metadata": {}, "dependencies": [],
            }
        rows = list(historical)
        if active_owner:
            rows.append({
                "id": "zz-current-owner", "status": "in_progress",
                "metadata": {"coordinator": fixture.target,
                             "gc.root_bead_id": "zz-current-root",
                             "gc.review_dispatch_root": "zz-current-review"},
                "dependencies": [],
            })
            fixture.linked_beads[("NewtonExperiments", "zz-current-root")] = {
                "id": "zz-current-root", "status": "in_progress", "metadata": {}, "dependencies": [],
            }
            fixture.linked_beads[("NewtonExperiments", "zz-current-review")] = {
                "id": "zz-current-review", "status": "in_progress", "metadata": {}, "dependencies": [],
            }
            fixture.blocked_review["metadata"]["gc.root_bead_id"] = "zz-current-root"
        fixture.owned_by_rig = {"NewtonExperiments": rows}

    def test_closed_terminal_history_does_not_starve_active_pause_or_review_budget(self):
        for historical_count in (12, 24):
            fixture = Fixture()
            try:
                self._install_closed_history_graph(fixture, historical_count)
                with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                    result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                   lambda _home: fixture.queue, fixture.now)
                self.assertTrue(any(item["type"] == "blocked_review"
                                    and item["bead_id"] == "blocked-review"
                                    for item in result["findings"]))
                self.assertTrue(any(item["type"] == "active_independent_review"
                                    and item["bead_id"] == "zz-current-review"
                                    for item in result["findings"]))
                pause_queries = [argv for argv, _timeout in fixture.calls
                                 if "gasvillage.pause_state=awaiting_review" in argv]
                self.assertTrue(any("gc.root_bead_id=zz-current-root" in argv
                                    for argv in pause_queries))
                lookups = [argv for argv, _timeout in fixture.calls if "--id" in argv]
                self.assertEqual(len(lookups), 2)
                self.assertEqual({argv[argv.index("--id") + 1] for argv in lookups},
                                 {"zz-current-root", "zz-current-review"})
                self.assertFalse(any("linked bead reads exceeded" in error["message"]
                                     for error in result["errors"]))
            finally:
                fixture.close()

    def test_closed_source_only_anchor_still_reaches_active_review_ahead_of_history(self):
        fixture = Fixture()
        try:
            self._install_closed_history_graph(fixture, 24, active_owner=False)
            source = {
                "id": "closed-current-source", "status": "closed",
                "metadata": {"coordinator": fixture.target,
                             "gc.review_dispatch_root": "still-active-review"},
                "dependencies": [],
            }
            fixture.owned_by_rig["NewtonExperiments"].append(source)
            fixture.linked_beads[("NewtonExperiments", "still-active-review")] = {
                "id": "still-active-review", "status": "in_progress", "metadata": {}, "dependencies": [],
            }
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "still-active-review"
                                for item in result["findings"]))
            lookups = [argv for argv, _timeout in fixture.calls if "--id" in argv]
            self.assertEqual(len(lookups), 1)
            self.assertEqual(lookups[0][lookups[0].index("--id") + 1], "still-active-review")
        finally:
            fixture.close()

    def test_reachable_retired_history_does_not_consume_current_pause_or_review_priority(self):
        fixture = Fixture()
        source = {
            "id": "zz-source", "status": "in_progress",
            "metadata": {"coordinator": fixture.target,
                         "gc.review_dispatch_root": "zz-current-review"},
            "dependencies": [],
        }
        current_review = {
            "id": "zz-current-review", "status": "in_progress",
            "metadata": {"coordinator": fixture.target}, "dependencies": [],
        }
        fixture.owned_by_rig = {"NewtonExperiments": [source, current_review]}
        fixture.blocked_review["metadata"]["gc.root_bead_id"] = "zz-current-review"
        for index in range(8):
            phase_id = f"historic-trigger-{index:02}"
            root_id = f"historic-root-{index:02}"
            review_id = f"historic-review-{index:02}"
            source["dependencies"].append({
                "issue_id": "zz-source", "depends_on_id": phase_id, "type": "blocks",
                "metadata": "{}",
            })
            fixture.linked_beads[("NewtonExperiments", phase_id)] = {
                "id": phase_id, "status": "closed",
                "metadata": {"gc.root_bead_id": root_id,
                             "gc.review_dispatch_root": review_id},
                "dependencies": [],
            }
            fixture.linked_beads[("NewtonExperiments", root_id)] = {
                "id": root_id, "status": "closed", "metadata": {"gc.outcome": "pass"},
                "dependencies": [],
            }
            fixture.linked_beads[("NewtonExperiments", review_id)] = {
                "id": review_id, "status": "closed",
                "metadata": {"gc.outcome": "skipped",
                             "gc.review.loop_outcome": "replan",
                             "work_outcome": "abandoned"},
                "dependencies": [],
            }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "blocked_review"
                                and item["bead_id"] == "blocked-review"
                                for item in result["findings"]))
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "zz-current-review"
                                for item in result["findings"]))
            self.assertFalse(any(item["type"] == "review_not_converged"
                                 and item["bead_id"].startswith("historic-review-")
                                 for item in result["findings"]))
            pause_queries = [argv for argv, _timeout in fixture.calls
                             if "gasvillage.pause_state=awaiting_review" in argv]
            self.assertEqual(len(pause_queries), 20)
            self.assertIn("gc.root_bead_id=zz-current-review", pause_queries[0])
            self.assertEqual(pause_queries[1][pause_queries[1].index("--parent") + 1],
                             "zz-current-review")
            lookups = [argv for argv, _timeout in fixture.calls if "--id" in argv]
            self.assertEqual(len(lookups), watchdog.MAX_GRAPH_LOOKUPS)
            self.assertFalse(any("linked bead reads exceeded" in error["message"]
                                 for error in result["errors"]))
            self.assertFalse(any(error["component"] == "linked-pause-scan"
                                and "current pause coverage is partial" in error["message"]
                                for error in result["errors"]))
        finally:
            fixture.close()

    def test_active_workflow_ref_bridge_reaches_current_review_before_closed_history(self):
        fixture = Fixture()
        fixture.blocked_review["metadata"]["gc.root_bead_id"] = "zz-current-review"
        fixture.owned_by_rig = {"NewtonExperiments": []}
        fixture.linked_beads[("experiments", "mx-book")] = {
            "id": "mx-book", "status": "closed", "metadata": {}, "dependencies": [],
        }
        fixture.request_rows = [{
            "id": "active-request", "parent": "mx-book", "labels": ["labtech-request"],
            "status": "open",
            "metadata": {"labtech.workflow_ref": "rig:NewtonExperiments/zz-current-implementation"},
            "dependencies": [],
        }]
        fixture.linked_beads[("NewtonExperiments", "zz-current-implementation")] = {
            "id": "zz-current-implementation", "status": "closed",
            "metadata": {"gc.review_dispatch_root": "zz-current-review"}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "zz-current-review")] = {
            "id": "zz-current-review", "status": "in_progress", "metadata": {}, "dependencies": [],
        }
        for index in range(24):
            owner = f"historic-owner-{index:02}"
            leaf = f"historic-source-{index:02}"
            fixture.owned_by_rig["NewtonExperiments"].append({
                "id": owner, "status": "closed",
                "metadata": {"coordinator": fixture.target, "source_task": leaf},
                "dependencies": [],
            })
            fixture.linked_beads[("NewtonExperiments", leaf)] = {
                "id": leaf, "status": "closed", "metadata": {}, "dependencies": [],
            }
        context = {
            "rig_names": ["experiments", "DeRham", "NewtonExperiments"],
            "rig": "experiments", "lab_book": "mx-book", "gc": fixture.gc, "city": fixture.city,
        }
        try:
            findings, errors = watchdog.coordinator_responsibility_findings(
                context, fixture.target, fixture.runner)
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "zz-current-review"
                                for item in findings))
            self.assertTrue(any(item["type"] == "blocked_review"
                                and item["bead_id"] == "blocked-review"
                                for item in findings))
            self.assertTrue(any("linked bead reads exceeded the 24-lookup bound" in error["message"]
                                for error in errors))
            lookups = [argv[argv.index("--id") + 1] for argv, _timeout in fixture.calls
                       if "--id" in argv]
            self.assertEqual(len(lookups), watchdog.MAX_GRAPH_LOOKUPS)
            self.assertEqual(lookups[:3], ["zz-current-implementation", "mx-book", "zz-current-review"])
            pause_queries = [argv for argv, _timeout in fixture.calls
                             if "gasvillage.pause_state=awaiting_review" in argv]
            self.assertTrue(any("gc.root_bead_id=zz-current-review" in argv
                                for argv in pause_queries))
        finally:
            fixture.close()

    def test_immediate_nudge_timeout_covers_measured_native_delivery_path(self):
        fixture = Fixture()
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                context = watchdog.discover_context(fixture.env(), fixture.runner)
            result = watchdog.send_nudge(context, fixture.target, "timing regression", fixture.runner)
            self.assertEqual(result["status"], "delivered_unverified")
            nudge_calls = [(argv, timeout) for argv, timeout in fixture.calls if "nudge" in argv]
            self.assertEqual(len(nudge_calls), 1)
            self.assertEqual(nudge_calls[0][1], 60)
            self.assertGreater(nudge_calls[0][1], 36.17)
            self.assertIn("--delivery", nudge_calls[0][0])
            self.assertIn("--json", nudge_calls[0][0])
        finally:
            fixture.close()

    def test_eight_reviewers_without_george_fit_nine_recipient_budget(self):
        fixture = Fixture()
        queue_config_path = fixture.home / "queue" / "config.json"
        queue_config = json.loads(queue_config_path.read_text())
        queue_config["reviewers"] = [f"reviewer-{index}" for index in range(watchdog.MAX_REVIEWERS)]
        queue_config_path.write_text(json.dumps(queue_config))
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertEqual(result["status"], "observed")
            self.assertEqual(len(result["notifications"]), watchdog.MAX_NUDGE_RECIPIENTS)
            nudge_targets = [call[call.index("nudge") + 1]
                             for call, _timeout in fixture.calls if "nudge" in call]
            self.assertEqual(len(nudge_targets), watchdog.MAX_NUDGE_RECIPIENTS)
            self.assertIn(fixture.target, nudge_targets)
            self.assertEqual(len(set(nudge_targets) - {fixture.target}), watchdog.MAX_REVIEWERS)
            self.assertTrue(all(timeout == watchdog.NUDGE_TIMEOUT for call, timeout in fixture.calls
                                if "nudge" in call))
        finally:
            fixture.close()

    def test_order_exec_quotes_pack_path_with_spaces(self):
        fixture = Fixture()
        try:
            script = fixture.pack / "bin" / "reconcile_watchdog.py"
            script.write_text("print('pack path resolved')\n")
            order = (Path(__file__).resolve().parents[1] / "orders" / "labtech-reconcile.toml").read_text()
            command = re.search(r"(?m)^exec\s*=\s*'(.+)'$", order).group(1)
            result = subprocess.run(["/bin/sh", "-c", command],
                                    env={**os.environ, "PACK_DIR": str(fixture.pack)},
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "pack path resolved")
        finally:
            fixture.close()

    def test_stale_root_and_blocked_review_repeat_without_task_creation_or_notification_flood(self):
        fixture = Fixture()
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                first = watchdog.run_observer(fixture.env(), fixture.runner,
                                              lambda _home: fixture.queue, fixture.now)
                second = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue,
                                               fixture.now + timedelta(hours=12))

            self.assertEqual(first["status"], "observed")
            self.assertEqual(second["status"], "observed")
            self.assertEqual(fixture.queue.reconcile_calls, 2)
            self.assertEqual(fixture.queue.run_next_calls, 0)
            self.assertEqual({item["type"] for item in first["findings"]},
                             {"stale_reconciliation", "coordinator_responsibility", "blocked_review"})
            self.assertEqual(len(first["notifications"]), 2)
            self.assertEqual(second["notifications"], [])
            self.assertTrue(all(item["status"] == "delivered_unverified"
                                for item in first["notifications"]))
            self.assertTrue(all("--delivery" in call and "immediate" in call and "--json" in call
                                for call, _timeout in fixture.calls if "nudge" in call))
            self.assertNotIn("gasvillage.reviewer", fixture.blocked_review["metadata"])
            blocked = next(item for item in first["findings"] if item["type"] == "blocked_review")
            self.assertEqual(blocked["root_bead_id"], "coordinator-root")
            self.assertEqual(blocked["assignee"], "NewtonExperiments/worker")
            self.assertFalse(any("create" in call[0] or "run-next" in call[0]
                                 for call in fixture.calls))
            self.assertFalse(any("update" in call[0] or "close" in call[0] or "assign" in call[0]
                                 for call in fixture.calls))
            config_calls = [call for call, _timeout in fixture.calls if "config" in call]
            self.assertTrue(any(str(fixture.city.resolve()) in call for call in config_calls))
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                context = watchdog.discover_context(fixture.env(), fixture.runner)
            self.assertEqual(context["pack"], fixture.pack.resolve())
            state = json.loads((fixture.home / "queue" / "watchdog-state.json").read_text())
            self.assertEqual(len(state["findings"]), 3)
            for record in state["findings"].values():
                self.assertEqual(record["notifications"]["experiments/labtech.george"]["status"],
                                 "delivered_unverified")
                self.assertTrue(record["notifications"]["experiments/labtech.george"]["native_result"]["delivery_attempted"])
        finally:
            fixture.close()

    def test_first_pause_without_reviewer_uses_bounded_linked_duty_metadata(self):
        fixture = Fixture()
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            blocked = next(item for item in result["findings"] if item["type"] == "blocked_review")
            self.assertNotIn("gasvillage.reviewer", fixture.blocked_review["metadata"])
            self.assertEqual(blocked["bead_id"], "blocked-review")
            self.assertEqual(blocked["root_bead_id"], "coordinator-root")
        finally:
            fixture.close()

    def test_dependency_row_limit_is_a_tracking_error_not_a_global_pause_limit(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [
            {"id": f"other-duty-{index}", "dependency_type": "blocks", "status": "closed"}
            for index in range(watchdog.MAX_LINKED_DUTIES)
        ] + [{"id": "late-review-duty", "dependency_type": "blocks", "status": "open"}]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "blocked_review" for item in result["findings"]))
            self.assertTrue(any(error["component"] == "responsibility-tracking" and
                                "dependency links; scan bound" in error["message"]
                                for error in result["errors"]))
            self.assertTrue(any(item.get("component") == "responsibility-tracking"
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_open_blocking_dependency_is_read_from_its_original_bead(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [
            {"id": "open-prerequisite", "dependency_type": "blocks"},
            {"id": "closed-prerequisite", "dependency_type": "blocks"},
        ]
        fixture.linked_beads[("NewtonExperiments", "open-prerequisite")] = {
            "id": "open-prerequisite", "title": "Open prerequisite", "status": "open",
            "metadata": {}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "closed-prerequisite")] = {
            "id": "closed-prerequisite", "title": "Closed prerequisite", "status": "closed",
            "metadata": {}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            prerequisite = next(item for item in result["findings"]
                                if item["type"] == "blocked_prerequisite")
            self.assertEqual(prerequisite["bead_id"], "open-prerequisite")
            self.assertEqual(prerequisite["status"], "open")
            self.assertFalse(any(item.get("bead_id") == "closed-prerequisite"
                                 and item["type"] == "blocked_prerequisite"
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_order_history_at_row_bound_reports_possible_truncation(self):
        fixture = Fixture()
        rows = [{"id": f"fresh-root-{index}", "title": "labtech-reconcile",
                 "status": "open", "assignee": None,
                 "created_at": watchdog.stamp(fixture.now - timedelta(hours=1))}
                for index in range(watchdog.MAX_ROWS)]
        rows.append({**fixture.stale_root, "id": "omitted-stale-root"})

        def runner(argv, timeout):
            if "bd" in argv and "list" in argv and "order-run:labtech-reconcile:rig:experiments" in argv:
                fixture.calls.append((list(argv), timeout))
                return CompletedProcess(argv, 0, json.dumps(rows[:watchdog.MAX_ROWS]), "")
            return fixture.runner(argv, timeout)

        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertEqual(result["status"], "error")
            self.assertTrue(any(error["component"] == "order-history" and
                                "reached the 100-row scan bound" in error["message"]
                                for error in result["errors"]))
            self.assertFalse(any(item.get("bead_id") == "omitted-stale-root"
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_attempt_receipt_scan_is_bounded_and_keeps_the_active_attempt(self):
        fixture = Fixture()
        attempts = fixture.queue.root / "attempts"
        for index in range(watchdog.MAX_ATTEMPT_ROWS + 5):
            (attempts / f"historical-{index:03}").mkdir()
        active_id = "zz-active-attempt"
        (attempts / active_id).mkdir()
        findings, errors = watchdog.receipt_findings(
            fixture.queue,
            {"active": {"attempt_id": active_id, "observation": "uncertain/interrupted"}},
        )
        try:
            missing = [item for item in findings if item["type"] == "missing_run_receipt"]
            self.assertLessEqual(len(missing), watchdog.MAX_ATTEMPT_ROWS)
            self.assertTrue(any(item.get("attempt_id") == active_id for item in missing))
            self.assertTrue(any(error["component"] == "receipt-scan" for error in errors))
        finally:
            fixture.close()

    def test_more_than_twelve_unrelated_pauses_do_not_hide_later_rig_responsibility(self):
        fixture = Fixture()
        fixture.unrelated_pauses = [
            {"id": f"unrelated-{index}", "status": "blocked",
             "metadata": {"gasvillage.pause_state": "awaiting_review",
                          "gc.root_bead_id": f"unrelated-root-{index}"}}
            for index in range(watchdog.MAX_PAUSE_CANDIDATES + 8)
        ]

        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item.get("bead_id") == "blocked-review"
                                and item["type"] == "blocked_review" for item in result["findings"]))
            self.assertFalse(any(item.get("bead_id", "").startswith("unrelated-")
                                 for item in result["findings"]))
            owner_queries = [argv for argv, _timeout in fixture.calls
                             if "bd" in argv and "list" in argv and
                             any(arg.startswith("coordinator=") for arg in argv)]
            self.assertTrue(any(argv[argv.index("--rig") + 1] == "NewtonExperiments"
                                and f"coordinator={fixture.target}" in argv for argv in owner_queries))
            pause_queries = [argv for argv, _timeout in fixture.calls
                             if "bd" in argv and "list" in argv and
                             "gasvillage.pause_state=awaiting_review" in argv]
            self.assertTrue(pause_queries)
            self.assertTrue(all(
                ("gc.root_bead_id=coordinator-root" in argv)
                or ("--parent" in argv and argv[argv.index("--parent") + 1] == "coordinator-root")
                for argv in pause_queries
            ))
        finally:
            fixture.close()

    def test_unlinked_first_pause_is_not_treated_as_a_city_wide_george_duty(self):
        fixture = Fixture()
        fixture.blocked_review["metadata"].pop("gc.root_bead_id")
        fixture.blocked_review["metadata"].pop("gc.root_store_ref")
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertFalse(any(item["type"] == "blocked_review" for item in result["findings"]))
        finally:
            fixture.close()

    def test_immediate_nudge_queued_without_delivery_is_retained_and_escalated_once(self):
        fixture = Fixture()
        fixture.stale_root["created_at"] = watchdog.stamp(fixture.now - timedelta(hours=13))
        fixture.blocked_review["created_at"] = watchdog.stamp(fixture.now - timedelta(hours=1))
        fixture.queue_nudge.add(fixture.target)
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                first = watchdog.run_observer(fixture.env(), fixture.runner,
                                              lambda _home: fixture.queue, fixture.now)
                second = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue,
                                               fixture.now + timedelta(hours=12))
            self.assertEqual(first["status"], "error")
            george_notice = next(item for item in first["notifications"]
                                 if item["target"] == fixture.target)
            self.assertEqual(george_notice["status"], "queued_unverified")
            self.assertFalse(george_notice["native_result"]["delivery_attempted"])
            self.assertEqual([item["target"] for item in first["notifications"]],
                             [fixture.target, "gascity.mayor"])
            self.assertEqual(first["notifications"][1]["status"], "delivered_unverified")
            self.assertEqual(second["notifications"], [])
            nudge_targets = [call[call.index("nudge") + 1]
                             for call, _timeout in fixture.calls if "nudge" in call]
            self.assertEqual(nudge_targets, [fixture.target, "gascity.mayor"])
            state = json.loads((fixture.home / "queue" / "watchdog-state.json").read_text())
            self.assertTrue(all(record["finding"].get("delivery_outcome")
                                for record in state["findings"].values()))
        finally:
            fixture.close()

    def test_queued_coordinator_delivery_remains_visible_without_duplicate_nudge(self):
        fixture = Fixture()
        fixture.queue_nudge.add("gascity.mayor")
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                first = watchdog.run_observer(fixture.env(), fixture.runner,
                                              lambda _home: fixture.queue, fixture.now)
                second = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue,
                                               fixture.now + timedelta(hours=12))
            self.assertEqual(first["status"], "error")
            self.assertEqual(second["status"], "error")
            self.assertEqual(second["notifications"], [])
            self.assertTrue(any(error.get("target") == "gascity.mayor" and
                                error.get("status") == "queued_unverified"
                                for error in second["errors"]))
            mayor_records = [record for record in json.loads(
                (fixture.home / "queue" / "watchdog-state.json").read_text()
            )["findings"].values() if "gascity.mayor" in record["notifications"]]
            self.assertTrue(mayor_records)
            self.assertTrue(all("gascity.mayor immediate nudge outcome remains queued_unverified" in
                                record["finding"]["delivery_outcome"] for record in mayor_records))
            nudge_targets = [call[call.index("nudge") + 1]
                             for call, _timeout in fixture.calls if "nudge" in call]
            self.assertEqual(nudge_targets, [fixture.target, "gascity.mayor"])
            for record in mayor_records:
                receipt = record["notifications"]["gascity.mayor"]
                self.assertEqual(receipt["status"], "queued_unverified")
                self.assertFalse(receipt["native_result"]["delivery_attempted"])
        finally:
            fixture.close()

    def test_closed_implementation_root_keeps_active_independent_review_visible(self):
        fixture = Fixture()
        fixture.coordinator_root.update({
            "status": "closed",
            "metadata": {"coordinator": fixture.target,
                         "gc.review_dispatch_root": "review-root",
                         "gc.root_store_ref": "rig:NewtonExperiments",
                         "gc.outcome": "pass"},
        })
        fixture.linked_beads[("NewtonExperiments", "review-root")] = {
            "id": "review-root", "title": "Independent review workflow",
            "status": "in_progress", "assignee": "NewtonExperiments/reviewer",
            "metadata": {"gc.outcome": "pass"}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            review = next(item for item in result["findings"]
                          if item["type"] == "active_independent_review")
            self.assertEqual(review["bead_id"], "review-root")
            self.assertEqual(review["assignee"], "NewtonExperiments/reviewer")
            self.assertEqual(review["source_workflow"], "rig:NewtonExperiments/coordinator-root")
        finally:
            fixture.close()

    def test_retired_predecessor_review_is_history_not_a_current_review_gate(self):
        cases = (
            ("in_progress", {}, "active_independent_review"),
            ("closed", {"gc.review.loop_outcome": "done",
                         "gc.review.synthesis_verdict": "pass",
                         "gc.review.test_result": "pass"}, None),
        )
        for current_status, current_metadata, expected_current_finding in cases:
            with self.subTest(current_status=current_status):
                fixture = Fixture()
                fixture.coordinator_root.update({
                    "status": "closed" if current_status == "closed" else "in_progress",
                    "metadata": {"coordinator": fixture.target,
                                 "predecessor_review": "retired-review",
                                 "gc.review_dispatch_root": "current-review"},
                })
                fixture.linked_beads[("NewtonExperiments", "retired-review")] = {
                    "id": "retired-review", "status": "closed",
                    "metadata": {"gc.outcome": "skipped",
                                 "gc.review.loop_outcome": "replan",
                                 "gc.review.synthesis_verdict": "pass_with_findings",
                                 "gc.review.test_result": "pass",
                                 "work_outcome": "abandoned"},
                    "dependencies": [],
                }
                fixture.linked_beads[("NewtonExperiments", "current-review")] = {
                    "id": "current-review", "status": current_status,
                    "metadata": current_metadata, "dependencies": [],
                }
                try:
                    with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                        result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                       lambda _home: fixture.queue, fixture.now)
                    review_findings = [item for item in result["findings"]
                                       if item["type"] in ("active_independent_review",
                                                           "blocked_review_workflow",
                                                           "review_not_converged")]
                    if expected_current_finding:
                        self.assertTrue(any(item["type"] == expected_current_finding
                                            and item["bead_id"] == "current-review"
                                            for item in review_findings))
                    else:
                        self.assertEqual(review_findings, [])
                    self.assertFalse(any(item.get("bead_id") == "retired-review"
                                         and item["type"] == "review_not_converged"
                                         for item in result["findings"]))
                    lookups = [argv[argv.index("--id") + 1] for argv, _timeout in fixture.calls
                               if "--id" in argv]
                    self.assertIn("retired-review", lookups)
                finally:
                    fixture.close()

    def test_closed_review_with_only_outcome_pass_is_not_treated_as_converged(self):
        fixture = Fixture()
        fixture.coordinator_root.update({
            "status": "closed",
            "metadata": {"coordinator": fixture.target,
                         "gc.review_dispatch_root": "review-root",
                         "gc.root_store_ref": "rig:NewtonExperiments",
                         "gc.outcome": "pass"},
        })
        fixture.linked_beads[("NewtonExperiments", "review-root")] = {
            "id": "review-root", "status": "closed",
            "metadata": {"gc.outcome": "pass"}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            finding = next(item for item in result["findings"]
                           if item["type"] == "review_not_converged")
            self.assertEqual(finding["bead_id"], "review-root")
            self.assertEqual(finding["review_loop_outcome"], None)
        finally:
            fixture.close()

    def test_current_skipped_replan_review_without_successor_remains_visible(self):
        for source_status in ("closed", "in_progress"):
            with self.subTest(source_status=source_status):
                fixture = Fixture()
                fixture.coordinator_root.update({
                    "status": source_status,
                    "metadata": {"coordinator": fixture.target,
                                 "gc.outcome": "pass",
                                 "gc.review_dispatch_root": "current-review"},
                })
                fixture.linked_beads[("NewtonExperiments", "current-review")] = {
                    "id": "current-review", "status": "closed",
                    "metadata": {"gc.outcome": "skipped",
                                 "gc.review.loop_outcome": "replan",
                                 "gc.review.synthesis_verdict": "fail",
                                 "gc.review.test_result": "fail"},
                    "dependencies": [],
                }
                try:
                    with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                        result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                       lambda _home: fixture.queue, fixture.now)
                    failed_review = next(item for item in result["findings"]
                                         if item["type"] == "review_not_converged")
                    self.assertEqual(failed_review["bead_id"], "current-review")
                    self.assertEqual(failed_review["review_loop_outcome"], "replan")
                    self.assertEqual(failed_review["synthesis_verdict"], "fail")
                    self.assertEqual(failed_review["test_result"], "fail")
                finally:
                    fixture.close()

    def setup_replan_successor(self, fixture, include_successor=True, first_pause=False):
        fixture.owned_by_rig = {"NewtonExperiments": [{
            "id": "closed-implementation", "status": "closed",
            "metadata": {"coordinator": fixture.target, "gc.outcome": "pass",
                         "gc.review_dispatch_root": "old-review"},
            "dependencies": [],
        }]}
        fixture.linked_beads[("NewtonExperiments", "old-review")] = {
            "id": "old-review", "status": "closed",
            "metadata": {"gc.outcome": "skipped",
                         "gc.review.loop_outcome": "replan",
                         "replan_successor": "replacement-workflow"},
            "dependencies": [],
        }
        if include_successor:
            fixture.linked_beads[("NewtonExperiments", "replacement-workflow")] = {
                "id": "replacement-workflow", "status": "in_progress",
                "assignee": "NewtonExperiments/worker",
                "metadata": {"gc.kind": "workflow"}, "dependencies": [],
            }
        if first_pause:
            fixture.blocked_review["metadata"]["gc.root_bead_id"] = "replacement-workflow"

    def test_replan_successor_inherits_workflow_duty_and_scans_first_pause(self):
        for first_pause in (False, True):
            with self.subTest(first_pause=first_pause):
                fixture = Fixture()
                self.setup_replan_successor(fixture, first_pause=first_pause)
                try:
                    with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                        result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                       lambda _home: fixture.queue, fixture.now)
                    successor_duties = [item for item in result["findings"]
                                        if item.get("bead_id") == "replacement-workflow"
                                        and item["type"] == "coordinator_responsibility"]
                    self.assertEqual(len(successor_duties), 1)
                    duty = successor_duties[0]
                    self.assertEqual(duty["responsibility_owner"], fixture.target)
                    self.assertEqual(duty["responsibility_source"], "inherited_successor")
                    self.assertEqual(duty["coordinator"], fixture.target)
                    self.assertIsNone(duty["coordinator_metadata_key"])
                    self.assertEqual(duty["assignee"], "NewtonExperiments/worker")
                    self.assertFalse(any(item["type"] == "active_independent_review"
                                         and item["bead_id"] == "replacement-workflow"
                                         for item in result["findings"]))
                    self.assertFalse(any(item["type"] == "review_not_converged"
                                         and item["bead_id"] == "old-review"
                                         for item in result["findings"]))
                    self.assertEqual(result["errors"], [])
                    pause_queries = [argv for argv, _timeout in fixture.calls
                                     if "gasvillage.pause_state=awaiting_review" in argv]
                    self.assertTrue(any("gc.root_bead_id=replacement-workflow" in argv
                                        for argv in pause_queries))
                    pause_findings = [item for item in result["findings"]
                                      if item["type"] == "blocked_review"
                                      and item["bead_id"] == "blocked-review"]
                    self.assertEqual(bool(pause_findings), first_pause)
                    if first_pause:
                        self.assertEqual(pause_findings[0]["root_bead_id"], "replacement-workflow")
                finally:
                    fixture.close()

    def test_unreadable_replan_successor_retains_review_and_tracking_error(self):
        fixture = Fixture()
        self.setup_replan_successor(fixture, include_successor=False)
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "review_not_converged"
                                and item["bead_id"] == "old-review"
                                for item in result["findings"]))
            self.assertTrue(any(item["type"] == "tracking_error"
                                and item.get("component") == "linked-bead"
                                and "replan_successor" in item["message"]
                                for item in result["findings"]))
            self.assertFalse(any(item.get("bead_id") == "replacement-workflow"
                                 and item["type"] == "coordinator_responsibility"
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_replan_successor_duty_deduplication_keeps_store_identity(self):
        fixture = Fixture()
        self.setup_replan_successor(fixture)
        fixture.owned_by_rig["DeRham"] = [{
            "id": "replacement-workflow", "status": "in_progress",
            "assignee": "DeRham/worker",
            "metadata": {"coordinator": fixture.target}, "dependencies": [],
        }]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            successor = next(item for item in result["findings"]
                             if item["type"] == "coordinator_responsibility"
                             and item["rig"] == "NewtonExperiments"
                             and item["bead_id"] == "replacement-workflow")
            other_store = next(item for item in result["findings"]
                               if item["type"] == "coordinator_responsibility"
                               and item["rig"] == "DeRham"
                               and item["bead_id"] == "replacement-workflow")
            self.assertEqual(successor["responsibility_source"], "inherited_successor")
            self.assertEqual(successor["assignee"], "NewtonExperiments/worker")
            self.assertEqual(other_store["responsibility_source"], "coordinator_metadata")
            self.assertEqual(other_store["assignee"], "DeRham/worker")
        finally:
            fixture.close()

    def test_current_replan_with_readable_successor_reports_successor_not_history(self):
        fixture = Fixture()
        fixture.coordinator_root.update({
            "status": "closed",
            "metadata": {"coordinator": fixture.target,
                         "gc.outcome": "pass",
                         "gc.review_dispatch_root": "old-review"},
        })
        fixture.linked_beads[("NewtonExperiments", "old-review")] = {
            "id": "old-review", "status": "closed",
            "metadata": {"gc.outcome": "skipped",
                         "gc.review.loop_outcome": "replan",
                         "gc.review.synthesis_verdict": "fail",
                         "gc.review.test_result": "fail",
                         "replan_successor": "current-successor"},
            "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "current-successor")] = {
            "id": "current-successor", "status": "in_progress",
            "metadata": {"gc.review_dispatch_root": "current-successor-review"}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "current-successor-review")] = {
            "id": "current-successor-review", "status": "in_progress",
            "metadata": {}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "current-successor-review"
                                for item in result["findings"]))
            self.assertFalse(any(item.get("bead_id") == "old-review"
                                 and item["type"] == "review_not_converged"
                                 for item in result["findings"]))
            self.assertFalse(any(item["type"] == "active_independent_review"
                                 and item["bead_id"] == "current-successor"
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_active_current_review_is_not_retired_by_successor_link(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"].update({"gc.review_dispatch_root": "current-review"})
        fixture.linked_beads[("NewtonExperiments", "current-review")] = {
            "id": "current-review", "status": "in_progress",
            "metadata": {"replan_successor": "new-work"}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "new-work")] = {
            "id": "new-work", "status": "open", "metadata": {}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "current-review"
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_review_requires_closed_loop_synthesis_and_test_pass(self):
        bead = {"status": "closed", "metadata": {"gc.outcome": "pass"}}
        self.assertFalse(watchdog.review_converged(bead))
        bead["metadata"].update({
            "gc.review.loop_outcome": "done",
            "gc.review.synthesis_verdict": "pass",
            "gc.review.test_result": "pass",
        })
        self.assertTrue(watchdog.review_converged(bead))

    def test_unsubmitted_labbook_request_without_workflow_ref_is_reported(self):
        fixture = Fixture()
        fixture.request_rows = [{
            "id": "request-preparing", "title": "Preparing a future run", "status": "open",
            "assignee": None, "parent": "mx-book", "labels": ["labtech-request"],
            "metadata": None, "dependencies": [],
        }]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "labtech_request"
                                and item["bead_id"] == "request-preparing"
                                for item in result["findings"]))
            self.assertTrue(any(item["type"] == "tracking_error"
                                and item["component"] == "request-link"
                                and item["bead_id"] == "request-preparing"
                                for item in result["findings"]))
            request_query = next(argv for argv, _timeout in fixture.calls
                                 if "labtech-request" in argv)
            self.assertIn("--parent", request_query)
            self.assertEqual(request_query[request_query.index("--parent") + 1], "mx-book")
            self.assertEqual(fixture.queue.run_next_calls, 0)
        finally:
            fixture.close()

    def test_request_workflow_ref_is_explicit_cross_rig_and_unreadable_link_is_visible(self):
        fixture = Fixture()
        fixture.request_rows = [{
            "id": "request-cross-rig", "title": "Request", "status": "open",
            "parent": "mx-book", "labels": ["labtech-request"],
            "metadata": {"labtech.workflow_ref": "rig:DeRham/missing-work"}, "dependencies": [],
        }]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            error = next(item for item in result["findings"]
                         if item["type"] == "tracking_error" and item["component"] == "linked-bead")
            self.assertEqual((error["rig"], error["bead_id"]), ("DeRham", "missing-work"))
            self.assertTrue(any(argv[argv.index("--rig") + 1] == "DeRham" and
                                argv[argv.index("--id") + 1] == "missing-work"
                                for argv, _timeout in fixture.calls if "--id" in argv))
        finally:
            fixture.close()

    def test_malformed_request_workflow_ref_is_a_tracking_error(self):
        fixture = Fixture()
        fixture.request_rows = [{
            "id": "request-bad-link", "title": "Request", "status": "open",
            "parent": "mx-book", "labels": ["labtech-request"],
            "metadata": {"labtech.workflow_ref": "not-a-store-reference"}, "dependencies": [],
        }]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "tracking_error"
                                and item["component"] == "request-link"
                                and "rig:<rig>/<bead-id>" in item["message"]
                                for item in result["findings"]))
            self.assertFalse(any("--id" in argv and "not-a-store-reference" in argv
                                 for argv, _timeout in fixture.calls))
        finally:
            fixture.close()

    def test_another_configured_coordinator_identity_is_used(self):
        fixture = Fixture()
        fixture.target = "experiments/labtech.jane"
        queue_config_path = fixture.home / "queue" / "config.json"
        queue_config = json.loads(queue_config_path.read_text())
        queue_config["target"] = fixture.target
        queue_config["reviewers"] = [fixture.target, "gascity.mayor"]
        queue_config_path.write_text(json.dumps(queue_config))
        fixture.coordinator_root["metadata"]["coordinator"] = fixture.target
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item.get("coordinator") == fixture.target
                                for item in result["findings"]))
            self.assertTrue(any(f"coordinator={fixture.target}" in argv
                                for argv, _timeout in fixture.calls if "--metadata-field" in argv))
        finally:
            fixture.close()

    def test_cycles_and_shared_roots_are_detected_and_deduplicated(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"]["source_task"] = "linked-node"
        second_owner = {
            "id": "second-owner", "status": "closed", "title": "Shared root",
            "metadata": {"coordinator": fixture.target,
                         "gc.root_bead_id": "coordinator-root",
                         "gc.root_store_ref": "rig:NewtonExperiments"},
            "dependencies": [],
        }
        fixture.owned_by_rig["NewtonExperiments"].append(second_owner)
        fixture.linked_beads[("NewtonExperiments", "linked-node")] = {
            "id": "linked-node", "status": "closed",
            "metadata": {"source_task": "coordinator-root"}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item.get("component") == "linked-bead" and "cycle detected" in item["message"]
                                for item in result["findings"]))
            pause_queries = [argv for argv, _timeout in fixture.calls
                             if "gasvillage.pause_state=awaiting_review" in argv]
            self.assertEqual(len(pause_queries), 2)
            self.assertEqual(sum("--id" in argv for argv, _timeout in fixture.calls), 1)
        finally:
            fixture.close()

    def test_native_finalizer_root_backlink_is_not_reported_as_a_cycle(self):
        fixture = Fixture()
        fixture.coordinator_root["status"] = "closed"
        fixture.coordinator_root["dependencies"] = [{
            "issue_id": "coordinator-root", "depends_on_id": "finalizer", "type": "tracks",
            "metadata": "{}",
        }]
        fixture.linked_beads[("NewtonExperiments", "finalizer")] = {
            "id": "finalizer", "status": "closed",
            "metadata": {"gc.root_bead_id": "coordinator-root",
                         "gc.root_store_ref": "rig:NewtonExperiments"},
            "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertFalse(any(item["type"] == "tracking_error"
                                 and item.get("component") == "linked-bead"
                                 and "cycle detected" in item.get("message", "")
                                 for item in result["findings"]))
            self.assertTrue(any(item["type"] == "blocked_review"
                                and item["bead_id"] == "blocked-review"
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_native_review_convoy_tracks_backlink_is_not_reported_as_a_cycle(self):
        fixture = Fixture()
        fixture.coordinator_root.update({
            "status": "closed",
            "metadata": {"coordinator": fixture.target,
                         "gc.review_dispatch_root": "current-review"},
            "dependencies": [{
                "issue_id": "coordinator-root", "depends_on_id": "finalizer", "type": "tracks",
                "metadata": "{}",
            }],
        })
        fixture.linked_beads[("NewtonExperiments", "finalizer")] = {
            "id": "finalizer", "status": "closed",
            "metadata": {"gc.root_bead_id": "coordinator-root",
                         "gc.review_dispatch_root": "current-review"},
            "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "current-review")] = {
            "id": "current-review", "status": "in_progress",
            "metadata": {"gc.input_convoy_id": "review-convoy"}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "review-convoy")] = {
            "id": "review-convoy", "issue_type": "convoy", "status": "closed", "metadata": {},
            "dependencies": [{
                "issue_id": "review-convoy", "depends_on_id": "coordinator-root", "type": "tracks",
                "metadata": "{}",
            }],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "active_independent_review"
                                and item["bead_id"] == "current-review"
                                for item in result["findings"]))
            self.assertFalse(any(item["type"] == "tracking_error"
                                 and item.get("component") == "linked-bead"
                                 and "cycle detected" in item.get("message", "")
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_typed_open_or_closed_input_convoy_tracks_source_without_false_cycle(self):
        for convoy_status in ("open", "closed"):
            with self.subTest(convoy_status=convoy_status):
                fixture = Fixture()
                fixture.coordinator_root["dependencies"] = [{
                    "issue_id": "coordinator-root", "depends_on_id": "implementation", "type": "blocks",
                    "metadata": "{}",
                }]
                fixture.linked_beads[("NewtonExperiments", "implementation")] = {
                    "id": "implementation", "status": "closed",
                    "metadata": {"source_task": "coordinator-root",
                                 "gc.input_convoy_id": "input-convoy"},
                    "dependencies": [],
                }
                fixture.linked_beads[("NewtonExperiments", "input-convoy")] = {
                    "id": "input-convoy", "issue_type": "convoy", "status": convoy_status,
                    "metadata": {},
                    "dependencies": [{
                        "issue_id": "input-convoy", "depends_on_id": "coordinator-root", "type": "tracks",
                        "metadata": "{}",
                    }],
                }
                try:
                    with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                        result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                       lambda _home: fixture.queue, fixture.now)
                    self.assertFalse(any(item["type"] == "tracking_error"
                                         and item.get("component") == "linked-bead"
                                         and "cycle detected" in item.get("message", "")
                                         for item in result["findings"]))
                finally:
                    fixture.close()

    def test_untyped_open_tracks_backlink_remains_a_cycle(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [{
            "issue_id": "coordinator-root", "depends_on_id": "implementation", "type": "blocks",
            "metadata": "{}",
        }]
        fixture.linked_beads[("NewtonExperiments", "implementation")] = {
            "id": "implementation", "status": "closed",
            "metadata": {"gc.input_convoy_id": "untyped-target"}, "dependencies": [],
        }
        fixture.linked_beads[("NewtonExperiments", "untyped-target")] = {
            "id": "untyped-target", "issue_type": "task", "status": "open",
            "metadata": {},
            "dependencies": [{
                "issue_id": "untyped-target", "depends_on_id": "coordinator-root", "type": "tracks",
                "metadata": "{}",
            }],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "tracking_error"
                                and item.get("component") == "linked-bead"
                                and "cycle detected" in item.get("message", "")
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_closed_implementation_source_task_backlink_to_blocking_source_is_membership(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [{
            "issue_id": "coordinator-root", "depends_on_id": "closed-implementation", "type": "blocks",
            "metadata": "{}",
        }]
        fixture.linked_beads[("NewtonExperiments", "closed-implementation")] = {
            "id": "closed-implementation", "status": "closed",
            "metadata": {"source_task": "coordinator-root"}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "coordinator_responsibility"
                                and item["bead_id"] == "coordinator-root"
                                for item in result["findings"]))
            self.assertFalse(any(item["type"] == "tracking_error"
                                 and item.get("component") == "linked-bead"
                                 and "cycle detected" in item.get("message", "")
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_closed_parent_backlink_to_tracks_owner_is_membership(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [{
            "issue_id": "coordinator-root", "depends_on_id": "closed-child", "type": "tracks",
            "metadata": "{}",
        }]
        fixture.linked_beads[("NewtonExperiments", "closed-child")] = {
            "id": "closed-child", "status": "closed", "parent": "coordinator-root",
            "metadata": {}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertFalse(any(item["type"] == "tracking_error"
                                 and item.get("component") == "linked-bead"
                                 and "cycle detected" in item.get("message", "")
                                 for item in result["findings"]))
        finally:
            fixture.close()

    def test_native_json_string_dependency_metadata_preserves_cross_rig_store(self):
        fixture = Fixture()
        fixture.coordinator_root["dependencies"] = [{
            "issue_id": "coordinator-root", "depends_on_id": "cross-blocker", "type": "blocks",
            "metadata": json.dumps({"gc.edge_store_ref": "rig:DeRham"}),
        }]
        fixture.linked_beads[("DeRham", "cross-blocker")] = {
            "id": "cross-blocker", "status": "blocked", "metadata": {}, "dependencies": [],
        }
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(item["type"] == "blocked_prerequisite"
                                and item["rig"] == "DeRham" and item["bead_id"] == "cross-blocker"
                                and item["status"] == "blocked"
                                for item in result["findings"]))
            self.assertTrue(any(argv[argv.index("--rig") + 1] == "DeRham"
                                and argv[argv.index("--id") + 1] == "cross-blocker"
                                for argv, _timeout in fixture.calls if "--id" in argv))
            self.assertFalse(any("cannot read explicit blocks link" in error["message"]
                                 for error in result["errors"]))
        finally:
            fixture.close()

    def test_malformed_or_unconfigured_native_dependency_store_metadata_is_reported(self):
        for raw_metadata, expected_message in (
            ("not-json", "malformed JSON dependency metadata"),
            (json.dumps({"gc.edge_store_ref": "rig:Unconfigured"}), "unconfigured rig store"),
        ):
            fixture = Fixture()
            fixture.coordinator_root["dependencies"] = [{
                "issue_id": "coordinator-root", "depends_on_id": "cross-blocker", "type": "blocks",
                "metadata": raw_metadata,
            }]
            try:
                with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                    result = watchdog.run_observer(fixture.env(), fixture.runner,
                                                   lambda _home: fixture.queue, fixture.now)
                self.assertTrue(any(item["type"] == "tracking_error"
                                    and expected_message in item["message"]
                                    for item in result["findings"]))
                self.assertFalse(any("--id" in argv and "cross-blocker" in argv
                                     for argv, _timeout in fixture.calls))
            finally:
                fixture.close()

    def test_owned_responsibility_row_budget_reports_partial_coverage(self):
        fixture = Fixture()
        fixture.owned_by_rig["NewtonExperiments"] = [
            {"id": f"owner-{index}", "status": "in_progress",
             "metadata": {"coordinator": fixture.target}, "dependencies": []}
            for index in range(watchdog.MAX_OWNED_ROWS + 1)
        ]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(error["component"] == "responsibility-seed-scan" and
                                "active coordinator responsibility scan exceeded" in error["message"]
                                for error in result["errors"]))
            self.assertTrue(any(item["type"] == "tracking_error"
                                and "active coordinator responsibility scan exceeded" in item["message"]
                                for item in result["findings"]))
        finally:
            fixture.close()

    def test_closed_owner_query_saturation_is_reported_before_link_filtering(self):
        fixture = Fixture()
        root_only_history = [
            {"id": f"old-owner-{index:02}", "status": "closed",
             "metadata": {"coordinator": fixture.target,
                          "gc.root_bead_id": f"old-root-{index:02}"},
             "dependencies": []}
            for index in range(watchdog.MAX_CLOSED_OWNED_ROWS + 1)
        ]
        current_source = {
            "id": "zz-closed-current-implementation", "status": "closed",
            "metadata": {"coordinator": fixture.target,
                         "gc.review_dispatch_root": "current-review"},
            "dependencies": [],
        }
        fixture.owned_by_rig["NewtonExperiments"] = [*root_only_history, current_source]

        def bounded_runner(argv, timeout):
            result = fixture.runner(argv, timeout)
            if "list" in argv and "--max-rows" in argv and result.returncode == 0:
                limit = int(argv[argv.index("--max-rows") + 1])
                rows = json.loads(result.stdout)
                return CompletedProcess(argv, 0, json.dumps(rows[:limit]), result.stderr)
            return result

        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), bounded_runner,
                                               lambda _home: fixture.queue, fixture.now)
            saturation = [error for error in result["errors"]
                          if error["component"] == "closed-responsibility-scan"
                          and "query reached its 65-row bound" in error["message"]]
            self.assertTrue(saturation)
            self.assertTrue(any(item["type"] == "tracking_error"
                                and item["component"] == "closed-responsibility-scan"
                                and "coverage is partial" in item["message"]
                                for item in result["findings"]))
            self.assertTrue(result["errors"])
        finally:
            fixture.close()

    def test_linked_workflow_root_budget_reports_partial_coverage(self):
        fixture = Fixture()
        fixture.owned_by_rig["NewtonExperiments"] = [
            {"id": f"owner-{index}", "status": "in_progress",
             "metadata": {"coordinator": fixture.target}, "dependencies": []}
            for index in range(watchdog.MAX_TRACKED_ROOTS + 1)
        ]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertTrue(any(error["component"] == "linked-pause-scan" and
                                "12-root bound" in error["message"]
                                for error in result["errors"]))
            self.assertTrue(any(error["component"] == "linked-pause-scan" and
                                "current pause coverage is partial" in error["message"]
                                for error in result["errors"]))
        finally:
            fixture.close()

    def test_uncertain_active_attempt_is_reported_without_releasing_or_relaunching(self):
        now = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
        checkpoint = {"active": {"attempt_id": "run-a1",
                                 "observation": "uncertain/interrupted; no exit receipt; manual disposition required"},
                      "requests": [], "attempts": []}
        fixture = Fixture(now=now, checkpoint=checkpoint)
        try:
            attempt = fixture.home / "queue" / "attempts" / "run-a1"
            attempt.mkdir()
            (attempt / "record.json").write_text("{}")
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, now)
            self.assertEqual(result["status"], "observed")
            receipt = next(item for item in result["findings"] if item["type"] == "missing_run_receipt")
            self.assertEqual(receipt["attempt_id"], "run-a1")
            self.assertTrue(receipt["active"])
            self.assertFalse((attempt / "exit.json").exists())
            self.assertEqual(fixture.queue.run_next_calls, 0)
            self.assertTrue(any("gascity.mayor" in call[0] for call in fixture.calls
                                if "nudge" in call[0]))
        finally:
            fixture.close()

    def test_store_query_error_is_reported_and_delivery_failure_is_retained(self):
        fixture = Fixture()
        fixture.fail_rig = "NewtonExperiments"
        fixture.fail_nudge.add("experiments/labtech.george")
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertEqual(result["status"], "error")
            self.assertTrue(any("NewtonExperiments" in error["message"] for error in result["errors"]))
            self.assertTrue(any(item["type"] == "tracking_error" and
                                item["rig"] == "NewtonExperiments" for item in result["findings"]))
            state = json.loads((fixture.home / "queue" / "watchdog-state.json").read_text())
            george_records = [record for record in state["findings"].values()]
            self.assertTrue(any(record["notifications"][fixture.target]["status"] == "unknown"
                                for record in george_records if fixture.target in record["notifications"]))
            self.assertEqual(fixture.queue.run_next_calls, 0)
        finally:
            fixture.close()

    def test_missing_effective_configuration_is_a_concrete_nonzero_diagnostic(self):
        fixture = Fixture()
        try:
            bad_config = {"ok": True, "config": {"Rigs": [{"Name": "experiments", "FormulaVars": {}}]}}

            def runner(argv, timeout):
                return CompletedProcess(argv, 0, json.dumps(bad_config), "")

            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                with self.assertRaisesRegex(watchdog.WatchdogError, "labtech_home"):
                    watchdog.discover_context(fixture.env(), runner)
        finally:
            fixture.close()

    def test_delivery_failure_retries_no_faster_than_next_tick(self):
        fixture = Fixture()
        fixture.fail_nudge.add(fixture.target)
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                first = watchdog.run_observer(fixture.env(), fixture.runner,
                                              lambda _home: fixture.queue, fixture.now)
                nudge_count = sum("nudge" in call for call, _timeout in fixture.calls)
                second = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue,
                                               fixture.now + timedelta(hours=6))
                self.assertEqual(sum("nudge" in call for call, _timeout in fixture.calls), nudge_count)
                third = watchdog.run_observer(fixture.env(), fixture.runner,
                                              lambda _home: fixture.queue,
                                              fixture.now + timedelta(hours=12))
            self.assertEqual(first["status"], "error")
            self.assertEqual(second["notifications"], [])
            self.assertEqual(second["status"], "error")
            self.assertTrue(any(error.get("target") == fixture.target and
                                error.get("status") == "unknown"
                                for error in second["errors"]))
            self.assertEqual(third["status"], "error")
            george = [notice for notice in third["notifications"] if notice["target"] == fixture.target]
            self.assertEqual(len(george), 1)
            self.assertEqual(george[0]["status"], "unknown")
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
