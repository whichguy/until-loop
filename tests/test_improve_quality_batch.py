"""No-model tests for append-only repeated-trial orchestration."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_batch as batch


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="improve-quality-batch-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.harness = self.base / "harness"
        (self.harness / "tests").mkdir(parents=True)
        for name in ("improve_quality.py", "improve_quality_resume.py"):
            (self.harness / "tests" / name).write_text("# frozen launcher\n", encoding="utf-8")
        self.source = self.base / "source"
        self.source.mkdir()

    def manifest(self, *, controlled: int = 0) -> tuple[Path, dict]:
        root = self.base / "batch"
        entries = batch.schedule(3, controlled)
        root.mkdir()
        value = {"format": batch.FORMAT, "root": str(root),
                 "harness": {"path": str(self.harness), "tree_sha256": batch.tree_digest(self.harness)},
                 "source": {"repository": str(self.source), "revision": "pinned"}, "schedule": entries}
        (root / batch.MANIFEST).write_text(json.dumps(value), encoding="utf-8")
        (root / batch.MANIFEST_FREEZE).write_text(json.dumps({
            "manifest_sha256": hashlib.sha256((root / batch.MANIFEST).read_bytes()).hexdigest()
        }), encoding="utf-8")
        (root / batch.ATTEMPTS).write_text("", encoding="utf-8")
        for entry in entries:
            if entry["cohort"] == "autonomous":
                trial = root / entry["relative_root"]
                trial.mkdir(parents=True)
                (trial / "trial.json").write_text("{}", encoding="utf-8")
        return root, value

    def test_schedule_has_twenty_four_autonomous_trials_and_separate_controlled_cohort(self) -> None:
        autonomous = batch.schedule(3, 0)
        combined = batch.schedule(3, 3)
        self.assertEqual(len(autonomous), 24)
        self.assertEqual({entry["case_id"] for entry in autonomous}, set(batch.AUTONOMOUS_CASES))
        self.assertTrue(all({"id", "case_id", "execution_mode", "root"} <= entry.keys() for entry in combined))
        self.assertEqual([entry["case_id"] for entry in autonomous[:8]], list(batch.AUTONOMOUS_CASES))
        self.assertTrue(all(case_id not in entry["root"] for entry in combined for case_id in batch.AUTONOMOUS_CASES))
        self.assertEqual([entry["id"] for entry in combined[-3:]], ["controlled-resume-01", "controlled-resume-02", "controlled-resume-03"])
        self.assertTrue(all(entry["cohort"] == "autonomous" for entry in autonomous))

    def test_prepare_builds_every_fixture_before_launch(self) -> None:
        root = self.base / "prepared"
        prepared = []
        def fake_prepare(entry, manifest, python):
            prepared.append(entry["id"])
            trial = Path(manifest["root"]) / entry["relative_root"]
            trial.mkdir(parents=True)
            (trial / "trial.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(batch, "_git_revision", return_value="pinned"), mock.patch.object(batch, "prepare_trial", side_effect=fake_prepare):
            manifest = batch.prepare_batch(root, self.harness, self.source, "tag", controlled_repetitions=3)
        self.assertEqual(len(prepared), 24)
        self.assertEqual(prepared, [entry["id"] for entry in manifest["schedule"] if entry["cohort"] == "autonomous"])
        self.assertTrue((root / batch.ATTEMPTS).is_file())

    def test_resume_never_relaunches_attempted_trials_and_failure_stops_new_dispatch(self) -> None:
        root, manifest = self.manifest()
        calls = []
        def fake_execute(root_value, entry, manifest_value, python):
            calls.append(entry["id"])
            return {"status": "incomplete" if entry["id"] == manifest["schedule"][0]["id"] else "pass"}
        with mock.patch.object(batch, "execute_trial", side_effect=fake_execute):
            first = batch.run_batch(root, concurrency=1)
            second = batch.run_batch(root, concurrency=1, continue_after_inspection=True)
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(calls[0], manifest["schedule"][0]["id"])
        self.assertNotEqual(calls[1], calls[0])
        records = batch._attempts(root)
        self.assertEqual(sum(record.get("kind") == "launched" and record["id"] == calls[0] for record in records), 1)
        self.assertGreater(len(second["launched_now"]), 0)

    def test_prior_nonpass_requires_explicit_continue_and_results_remain_append_only(self) -> None:
        root, manifest = self.manifest(controlled=3)
        batch._append(root, {"kind": "launched", "id": manifest["schedule"][0]["id"]})
        batch._append(root, {"kind": "finished", "id": manifest["schedule"][0]["id"], "status": "fail"})
        before = (root / batch.ATTEMPTS).read_bytes()
        result = batch.run_batch(root, concurrency=1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual((root / batch.ATTEMPTS).read_bytes(), before)

    def test_partial_prepare_and_unknown_attempt_cannot_be_counted_as_complete(self) -> None:
        root, manifest = self.manifest()
        (root / manifest["schedule"][0]["relative_root"] / "trial.json").unlink()
        with self.assertRaisesRegex(batch.BatchError, "not completely prepared"):
            batch.run_batch(root)
        trial = root / manifest["schedule"][0]["relative_root"]
        (trial / "trial.json").write_text("{}", encoding="utf-8")
        batch._append(root, {"kind": "launched", "id": "not-in-manifest"})
        with self.assertRaisesRegex(batch.BatchError, "outside this manifest"):
            batch.run_batch(root)

    def test_worker_uses_frozen_launchers_and_does_not_mix_controlled_mode(self) -> None:
        root, manifest = self.manifest(controlled=1)
        autonomous = manifest["schedule"][0]
        controlled = manifest["schedule"][-1]
        calls = []

        def fake_command(argv, cwd=None, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(batch, "command", side_effect=fake_command), mock.patch.object(batch, "_grade_status", return_value="pass"):
            self.assertEqual(batch.execute_trial(root, autonomous, manifest, "python-test")["status"], "pass")
            self.assertEqual(batch.execute_trial(root, controlled, manifest, "python-test")["status"], "pass")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0][2:4], ["run", "--root"])
        self.assertIn("1200", calls[0][0])
        self.assertEqual(calls[1][0][2:4], ["audit", "--root"])
        self.assertIn("900", calls[1][0])
        self.assertEqual(calls[2][0][2], "run-all")
        self.assertNotIn("audit", calls[2][0])
        self.assertGreaterEqual(calls[2][1]["timeout"], 1200 + 1200 + 600)

    def test_frozen_harness_drift_prevents_any_host_launch(self) -> None:
        root, manifest = self.manifest()
        (self.harness / "tests/improve_quality.py").write_text("# changed launcher\n")
        with mock.patch.object(batch, "execute_trial") as execute:
            with self.assertRaisesRegex(batch.BatchError, "frozen harness drifted"):
                batch.run_batch(root)
        execute.assert_not_called()
        self.assertEqual(batch._attempts(root), [])

    def test_manifest_edit_cannot_redirect_queued_work(self) -> None:
        root, manifest = self.manifest()
        manifest["schedule"][0]["case_id"] = "tenant_cache"
        (root / batch.MANIFEST).write_text(json.dumps(manifest))
        with mock.patch.object(batch, "execute_trial") as execute:
            with self.assertRaisesRegex(batch.BatchError, "manifest changed"):
                batch.run_batch(root)
        execute.assert_not_called()
        self.assertEqual(batch._attempts(root), [])

    def test_invalid_terminal_receipts_cannot_complete_or_resume(self) -> None:
        root, manifest = self.manifest()
        first_id = manifest["schedule"][0]["id"]
        for status in (None, "completed", True):
            with self.subTest(status=status):
                records = [{"kind": "launched", "id": first_id},
                           {"kind": "finished", "id": first_id, "status": status}]
                (root / batch.ATTEMPTS).write_text("\n".join(map(json.dumps, records)) + "\n")
                with mock.patch.object(batch, "execute_trial") as execute:
                    with self.assertRaisesRegex(batch.BatchError, "invalid terminal status"):
                        batch.run_batch(root, continue_after_inspection=True)
                execute.assert_not_called()

    def test_schedule_path_and_cohort_remain_checked_even_with_matching_digest(self) -> None:
        root, manifest = self.manifest()
        for changes in ({"root": "../sibling", "relative_root": "../sibling"},
                        {"execution_mode": "controlled_resume"}):
            with self.subTest(changes=changes):
                altered = json.loads(json.dumps(manifest))
                altered["schedule"][0].update(changes)
                (root / batch.MANIFEST).write_text(json.dumps(altered))
                (root / batch.MANIFEST_FREEZE).write_text(json.dumps({
                    "manifest_sha256": hashlib.sha256((root / batch.MANIFEST).read_bytes()).hexdigest()
                }))
                with self.assertRaisesRegex(batch.BatchError, "schedule"):
                    batch.run_batch(root)

    def test_orphaned_launch_is_never_reexecuted_after_inspection(self) -> None:
        root, manifest = self.manifest()
        first_id = manifest["schedule"][0]["id"]
        batch._append(root, {"kind": "launched", "id": first_id})
        self.assertEqual(batch.run_batch(root)["status"], "stopped")
        calls = []
        def fake_execute(root_value, entry, manifest_value, python):
            calls.append(entry["id"])
            return {"status": "pass"}
        with mock.patch.object(batch, "execute_trial", side_effect=fake_execute):
            result = batch.run_batch(root, concurrency=1, continue_after_inspection=True)
        self.assertNotIn(first_id, calls)
        self.assertEqual(result["unfinished"], [first_id])
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(any(r["kind"] == "finished" and r["id"] == first_id for r in batch._attempts(root)))

    def test_selected_grade_file_is_authoritative(self) -> None:
        root, manifest = self.manifest()
        entry = manifest["schedule"][0]
        evidence = root / entry["relative_root"] / "evidence"
        (evidence / "audit").mkdir(parents=True)
        (evidence / "audit-selection.json").write_text(
            json.dumps({"directory": "audit", "grade_file": "final/grade.json"}), encoding="utf-8")
        (evidence / "audit" / "grade.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        (evidence / "audit" / "final").mkdir()
        (evidence / "audit" / "final" / "grade.json").write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
        self.assertEqual(batch._grade_status(root, entry), "incomplete")

    def test_status_only_pass_cannot_release_another_trial(self) -> None:
        root, manifest = self.manifest()
        entry = manifest["schedule"][0]
        evidence = root / entry["root"] / "evidence"
        (evidence / "audit").mkdir(parents=True)
        (evidence / "audit-selection.json").write_text(json.dumps({"directory": "audit"}))
        (evidence / "audit/grade.json").write_text(json.dumps({"status": "pass"}))
        self.assertEqual(batch._grade_status(root, entry), "incomplete")

    def test_sibling_audit_cannot_supply_a_passing_grade(self) -> None:
        root, manifest = self.manifest()
        entry = manifest["schedule"][0]
        trial = root / entry["root"]
        evidence = trial / "evidence"
        evidence.mkdir()
        (trial / "foreign-audit").mkdir()
        (trial / "foreign-audit/grade.json").write_text(json.dumps({
            "status": "pass", "schema_valid": True, "sequence": {}, "outcome": {}
        }))
        (evidence / "audit-selection.json").write_text(json.dumps({"directory": "../foreign-audit"}))
        self.assertEqual(batch._grade_status(root, entry), "incomplete")

    def test_nonblocking_lock_refuses_an_overlapping_launcher(self) -> None:
        root, manifest = self.manifest()
        entered, release = threading.Event(), threading.Event()

        def blocked_execute(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "pass"}

        with mock.patch.object(batch, "execute_trial", side_effect=blocked_execute):
            worker = threading.Thread(target=batch.run_batch, kwargs={"root": root, "concurrency": 1})
            worker.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(batch.BatchError, "already running"):
                batch.run_batch(root, concurrency=1)
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_operator_stop_marker_prevents_new_dispatch_without_deleting_it(self) -> None:
        root, manifest = self.manifest()
        calls = []

        def fake_execute(root_value, entry, manifest_value, python):
            calls.append(entry["id"])
            (root / batch.STOP_DISPATCH).touch()
            return {"status": "pass"}

        with mock.patch.object(batch, "execute_trial", side_effect=fake_execute):
            result = batch.run_batch(root, concurrency=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["reason"], "external stop-dispatch marker")
        self.assertTrue((root / batch.STOP_DISPATCH).is_file())

    def test_direct_import_works_from_a_foreign_working_directory(self) -> None:
        code = "import os, sys; os.chdir('/'); sys.path.insert(0, %r); import improve_quality_batch; print(improve_quality_batch.FORMAT)" % str(TESTS_DIR)
        result = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), batch.FORMAT)


if __name__ == "__main__":
    unittest.main()
