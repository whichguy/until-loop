"""Recovery evidence checks without invoking a live model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import improve_quality as quality
from tests import improve_quality_recover as recover
from tests import improve_quality_grading as grading


class ObserverRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def source(self) -> Path:
        source = self.base / "source"
        source.mkdir()
        quality.command(source, "git", "init", "-q")
        quality.git(source, "config", "user.name", "Fixture")
        quality.git(source, "config", "user.email", "fixture@example.invalid")
        package = source / "skills" / "improve"
        (package / "references").mkdir(parents=True)
        (package / "runtime" / "until-loop").mkdir(parents=True)
        (package / "SKILL.md").write_text("Standalone owner binding\n", encoding="utf-8")
        (package / "references" / "review-policy.md").write_text("Policy ID: improve/review-policy/v1\n", encoding="utf-8")
        (package / "runtime" / "until-loop" / "ADAPTER.md").write_text("Interpret the contract using until-loop\n", encoding="utf-8")
        quality.git(source, "add", "skills")
        quality.git(source, "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "seed")
        return source

    def prepared(self) -> Path:
        root = self.base / "trial"
        quality.prepare(self.source(), "HEAD", root, "blank_fallback")
        evidence = root / "evidence"
        (evidence / "invocation.json").write_text(json.dumps({"invocation_count": 1}), encoding="utf-8")
        source = root / "package"
        markers = "Standalone owner binding Policy ID: improve/review-policy/v1 Interpret the contract until-loop"
        event = {"type": "item.completed", "item": {"command": "cat " + str(source), "exit_code": 0, "aggregated_output": markers}}
        (evidence / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        return root

    def test_recovery_keeps_candidate_and_marks_trial_incomplete(self) -> None:
        root = self.prepared()
        workspace = root / "candidate"
        before = quality.tree(workspace)
        with mock.patch.object(recover, "active_host_processes", return_value=[]):
            observed = recover.recover(root, "observer crashed after accepted state")
        self.assertEqual(quality.tree(workspace), before)
        self.assertEqual(observed["trial_status"], "incomplete")
        self.assertIsNone(observed["returncode"])
        self.assertEqual(observed["recovery"]["original_process_result"], "unknown")
        self.assertTrue(observed["recovery"]["candidate_state_history_unchanged"])
        self.assertEqual(observed["snapshots"][-1]["id"], "s0001")
        self.assertTrue(observed["snapshots"][-1]["stable"])
        self.assertTrue((root / "evidence" / "observed.json").is_file())

        judgment = {"auditor_confidence": "supported", "unresolved_material_findings": False, "reviews": [
            {"review_id": "one", "snapshot_id": "s0001", "classification": "qualifying", "evidence_refs": ["final-oracle.json"], "finding_summary": "clean", "checks_current": True, "distinct_review": True, "required_commit_satisfied": True},
            {"review_id": "two", "snapshot_id": "s0001", "classification": "qualifying", "evidence_refs": ["final-oracle.json"], "finding_summary": "clean again", "checks_current": True, "distinct_review": True, "required_commit_satisfied": True},
        ]}
        self.assertNotEqual(grading.validate_and_grade(judgment, observed)["status"], "pass")

    def test_recovery_refuses_missing_marker_existing_observation_and_active_host(self) -> None:
        root = self.prepared()
        evidence = root / "evidence"
        (evidence / "invocation.json").unlink()
        with self.assertRaisesRegex(recover.RecoveryError, "invocation marker"):
            recover.recover(root, "crash")

        (evidence / "invocation.json").write_text("{}", encoding="utf-8")
        (evidence / "observed.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(recover.RecoveryError, "overwrite"):
            recover.recover(root, "crash")

        (evidence / "observed.json").unlink()
        with mock.patch.object(recover, "active_host_processes", return_value=[{"pid": "1", "command": "codex --cd candidate"}]):
            with self.assertRaisesRegex(recover.RecoveryError, "host is active"):
                recover.recover(root, "crash")


if __name__ == "__main__":
    unittest.main()
