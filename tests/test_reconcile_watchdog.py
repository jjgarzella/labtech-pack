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
            "created_at": watchdog.stamp(self.now - timedelta(days=7)),
            "updated_at": watchdog.stamp(self.now - timedelta(hours=1)),
            "metadata": {"gasvillage.pause_state": "awaiting_review",
                         "gc.root_bead_id": "coordinator-root",
                         "gc.root_store_ref": "rig:NewtonExperiments",
                         "gasvillage.review_condition": "scope_clarified"},
        }
        self.coordinator_root = {
            "id": "coordinator-root", "status": "in_progress",
            "metadata": {"coordinator": self.target},
            "dependencies": [], "dependents": [],
        }
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
                return CompletedProcess(argv, 0, json.dumps([self.stale_root]), "")
            if "--metadata-field" in argv and rig == "NewtonExperiments":
                return CompletedProcess(argv, 0, json.dumps([self.blocked_review]), "")
            return CompletedProcess(argv, 0, "[]", "")
        if "bd" in argv and "show" in argv:
            bead_id = argv[argv.index("show") + 1]
            if bead_id == self.coordinator_root["id"]:
                return CompletedProcess(argv, 0, json.dumps([self.coordinator_root]), "")
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
        self.assertRegex(order, r'(?m)^timeout\s*=\s*"900s"$')
        self.assertIn('exec = \'python3 "$PACK_DIR/bin/reconcile_watchdog.py"\'', order)
        self.assertRegex(order, r'(?m)^no_work_gate\s*=\s*true$')
        self.assertNotRegex(order, r'(?m)^formula\s*=')
        self.assertIn("12-hour", formula)
        self.assertGreater(watchdog.NUDGE_TIMEOUT, 36.17)
        self.assertEqual(watchdog.NUDGE_TIMEOUT, 60)
        self.assertEqual(watchdog.ORDER_TIMEOUT_SECONDS, 900)
        self.assertEqual(watchdog.MAX_EXTERNAL_COMMAND_SECONDS, 680)
        self.assertLessEqual(watchdog.MAX_EXTERNAL_COMMAND_SECONDS,
                             watchdog.ORDER_TIMEOUT_SECONDS)

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
                             {"stale_reconciliation", "blocked_review"})
            self.assertEqual(len(first["notifications"]), 2)
            self.assertEqual(second["notifications"], [])
            self.assertTrue(all(item["status"] == "delivered_unverified"
                                for item in first["notifications"]))
            self.assertTrue(all("--delivery" in call and "immediate" in call and "--json" in call
                                for call, _timeout in fixture.calls if "nudge" in call))
            self.assertNotIn("gasvillage.reviewer", fixture.blocked_review["metadata"])
            blocked = next(item for item in first["findings"] if item["type"] == "blocked_review")
            self.assertEqual(blocked["basis"], "root_coordinator")
            self.assertEqual(blocked["coordinator_bead_id"], "coordinator-root")
            self.assertFalse(any("create" in call[0] or "run-next" in call[0]
                                 for call in fixture.calls))
            config_calls = [call for call, _timeout in fixture.calls if "config" in call]
            self.assertTrue(any(str(fixture.city.resolve()) in call for call in config_calls))
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                context = watchdog.discover_context(fixture.env(), fixture.runner)
            self.assertEqual(context["pack"], fixture.pack.resolve())
            state = json.loads((fixture.home / "queue" / "watchdog-state.json").read_text())
            self.assertEqual(len(state["findings"]), 2)
            for record in state["findings"].values():
                self.assertEqual(record["notifications"]["experiments/labtech.george"]["status"],
                                 "delivered_unverified")
                self.assertTrue(record["notifications"]["experiments/labtech.george"]["native_result"]["delivery_attempted"])
        finally:
            fixture.close()

    def test_first_pause_without_reviewer_uses_bounded_linked_duty_metadata(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"] = {"title": "workflow root"}
        fixture.coordinator_root["dependencies"] = [
            {"id": "review-duty", "metadata": {"gc.coordinator": fixture.target}}
        ]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            blocked = next(item for item in result["findings"] if item["type"] == "blocked_review")
            self.assertNotIn("gasvillage.reviewer", fixture.blocked_review["metadata"])
            self.assertEqual(blocked["basis"], "linked_duty")
            self.assertEqual(blocked["coordinator_bead_id"], "review-duty")
        finally:
            fixture.close()

    def test_late_linked_coordinator_surfaces_bounded_scan_error(self):
        fixture = Fixture()
        fixture.coordinator_root["metadata"] = {"title": "workflow root"}
        fixture.coordinator_root["dependencies"] = [
            {"id": f"other-duty-{index}", "metadata": {"coordinator": "another/session"}}
            for index in range(watchdog.MAX_LINKED_DUTIES)
        ] + [{"id": "late-review-duty", "metadata": {"coordinator": fixture.target}}]
        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), fixture.runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertFalse(any(item["type"] == "blocked_review" for item in result["findings"]))
            self.assertTrue(any(error["component"] == "review-root" and
                                "bounded scan stops at 8" in error["message"]
                                for error in result["errors"]))
            self.assertTrue(any(item.get("component") == "review-root"
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
            if "bd" in argv and "list" in argv and "--label" in argv:
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

    def test_blocked_pause_query_at_row_bound_reports_possible_truncation(self):
        fixture = Fixture()
        rows = [{"id": f"blocked-{index}", "title": "awaiting review",
                 "status": "blocked", "assignee": "worker",
                 "metadata": {"gasvillage.pause_state": "awaiting_review"}}
                for index in range(watchdog.MAX_ROWS)]
        rows.append({"id": "omitted-pause", "status": "blocked",
                     "metadata": {"gasvillage.pause_state": "awaiting_review"}})

        def runner(argv, timeout):
            if "bd" in argv and "list" in argv and "--metadata-field" in argv:
                rig = argv[argv.index("--rig") + 1]
                if rig == "NewtonExperiments":
                    fixture.calls.append((list(argv), timeout))
                    return CompletedProcess(argv, 0, json.dumps(rows[:watchdog.MAX_ROWS]), "")
            return fixture.runner(argv, timeout)

        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertEqual(result["status"], "error")
            self.assertTrue(any(error["component"] == "review-scan" and
                                "reached the 100-row scan bound" in error["message"]
                                for error in result["errors"]))
            self.assertFalse(any(item.get("bead_id") == "omitted-pause"
                                 for item in result["findings"]))
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
            self.assertTrue(all("gascity.mayor immediate nudge outcome remains queued_unverified" in
                                record["finding"]["delivery_outcome"]
                                for record in json.loads(
                                    (fixture.home / "queue" / "watchdog-state.json").read_text()
                                )["findings"].values()))
            nudge_targets = [call[call.index("nudge") + 1]
                             for call, _timeout in fixture.calls if "nudge" in call]
            self.assertEqual(nudge_targets, [fixture.target, "gascity.mayor"])
            state = json.loads((fixture.home / "queue" / "watchdog-state.json").read_text())
            for record in state["findings"].values():
                receipt = record["notifications"]["gascity.mayor"]
                self.assertEqual(receipt["status"], "queued_unverified")
                self.assertFalse(receipt["native_result"]["delivery_attempted"])
        finally:
            fixture.close()

    def test_scan_stops_after_twelve_durable_pause_root_lookups(self):
        fixture = Fixture()
        pauses = []
        root_lookups = []
        for index in range(watchdog.MAX_PAUSE_CANDIDATES + 1):
            pauses.append({**fixture.blocked_review,
                           "id": f"blocked-{index}",
                           "metadata": {**fixture.blocked_review["metadata"],
                                        "gc.root_bead_id": f"root-{index}"}})

        def runner(argv, timeout):
            if argv[0] == str(fixture.gc) and "config" in argv and "show" in argv:
                return CompletedProcess(argv, 0, json.dumps(fixture.effective_config), "")
            if "bd" in argv and "list" in argv:
                rig = argv[argv.index("--rig") + 1]
                if rig == fixture.fail_rig:
                    return CompletedProcess(argv, 2, "", "store unavailable")
                if "--label" in argv:
                    return CompletedProcess(argv, 0, json.dumps([fixture.stale_root]), "")
                if "--metadata-field" in argv and rig == "NewtonExperiments":
                    return CompletedProcess(argv, 0, json.dumps(pauses), "")
                return CompletedProcess(argv, 0, "[]", "")
            if "bd" in argv and "show" in argv:
                bead_id = argv[argv.index("show") + 1]
                root_lookups.append(bead_id)
                return CompletedProcess(argv, 0, json.dumps([{
                    "id": bead_id, "metadata": {"coordinator": fixture.target},
                    "dependencies": [], "dependents": [],
                }]), "")
            return fixture.runner(argv, timeout)

        try:
            with patch.object(watchdog.shutil, "which", return_value=str(fixture.gc)):
                result = watchdog.run_observer(fixture.env(), runner,
                                               lambda _home: fixture.queue, fixture.now)
            self.assertEqual(len(root_lookups), watchdog.MAX_PAUSE_CANDIDATES)
            self.assertTrue(any(error["component"] == "review-scan" for error in result["errors"]))
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
            self.assertTrue(any(item["type"] == "store_read_error" and
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
