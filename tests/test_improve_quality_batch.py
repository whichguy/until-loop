"""No-model tests for append-only repeated-trial orchestration."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import improve_quality_batch as batch


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="improve-quality-batch-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
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

        def fake_command(argv, cwd=None):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(batch, "command", side_effect=fake_command), mock.patch.object(batch, "_grade_status", return_value="pass"):
            self.assertEqual(batch.execute_trial(root, autonomous, manifest, "python-test")["status"], "pass")
            self.assertEqual(batch.execute_trial(root, controlled, manifest, "python-test")["status"], "pass")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][2:4], ["run", "--root"])
        self.assertIn("1200", calls[0])
        self.assertEqual(calls[1][2:4], ["audit", "--root"])
        self.assertIn("900", calls[1])
        self.assertEqual(calls[2][2], "run-all")
        self.assertNotIn("audit", calls[2])


if __name__ == "__main__":
    unittest.main()
