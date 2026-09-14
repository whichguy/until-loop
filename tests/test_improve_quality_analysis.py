"""Evidence analysis tests; synthetic records do not demonstrate live behavior."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_analysis as analysis


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def oracle(**values):
    return {"passed": all(v is True for v in values.values()),
            "assertions": {k: {"passed": v} for k, v in values.items()}}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def trial(self, root=None, mode="autonomous"):
        root = root or self.root / "trial"
        evidence = root / "evidence"
        write(root / "trial.json", {"case_id": "blank_fallback", "evidence": str(evidence),
              "execution_mode": mode, "fixture": {"scope_paths": ["formatter.py"]}})
        snapshots = [
            {"id": "a", "stable": True, "candidate_digest": "digest-a",
             "manifest": {"formatter.py": {"sha256": "old"}}},
            {"id": "b", "stable": True, "candidate_digest": "digest-b",
             "manifest": {"formatter.py": {"sha256": "new"}}},
        ]
        write(evidence / "observed.json", {"trial_status": "completed", "snapshots": snapshots})
        write(evidence / "audit/observed.json", {"trial_status": "completed", "snapshots": snapshots})
        write(evidence / "snapshot-oracles.json", {"a": oracle(blank=False, name=True), "b": oracle(blank=True, name=True)})
        write(evidence / "final-oracle.json", oracle(blank=True, name=True))
        write(evidence / "audit/grade.json", {"status": "pass", "sequence": {
            "states": [{"review_id": "unproven", "snapshot_id": "a", "completed": False,
                        "classification": "unknown", "streak_after": 0},
                       {"review_id": "first", "snapshot_id": "a", "completed": True,
                        "classification": "material", "streak_after": 0,
                        "candidate_digest": "digest-a", "snapshot_stable": True},
                       {"review_id": "last", "snapshot_id": "b", "completed": True,
                        "classification": "qualifying", "streak_after": 2,
                        "candidate_digest": "digest-b", "snapshot_stable": True}],
            "completed_review_count": 2, "final_observed_snapshot_id": "b",
            "final_observed_candidate_digest": "digest-b",
            "final_observed_snapshot_stable": True,
            "final_candidate_bound": True}})
        return root

    def test_comparison_preserves_repairs_and_regressions(self):
        result = analysis.compare_oracles(oracle(a=False, b=True), oracle(a=True, b=False))
        self.assertTrue(result["comparable"])
        self.assertEqual(result["newly_passing"], ["a"])
        self.assertEqual(result["regressed"], ["b"])

    def test_missing_assertions_are_unknown_not_repairs(self):
        result = analysis.compare_oracles(oracle(a=False), oracle(a=True, b=True))
        self.assertFalse(result["comparable"])
        self.assertEqual(result["unknown"], ["b"])
        self.assertEqual(result["newly_passing"], ["a"])
        self.assertFalse(analysis.compare_oracles({}, {})["comparable"])

    def test_first_completed_review_uses_snapshot_oracle_and_scope(self):
        root = self.trial()
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        row = analysis.analyze_trial(root)
        self.assertEqual(row["comparison"]["first_completed_review"], "first")
        self.assertEqual(row["comparison"]["newly_passing"], ["blank"])
        self.assertEqual(row["comparison"]["changed_scope_paths"], ["formatter.py"])
        self.assertEqual(row["comparison"]["oracle_sources"]["final"], "snapshot-oracles:b")
        self.assertIn(str(root / "evidence/audit/observed.json"), row["evidence_sha256"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_controlled_trial_selects_combined_audit(self):
        root = self.trial(mode="controlled_resume")
        write(root / "evidence/audit/grade.json", {"status": "incomplete"})
        self.trial(root / "controlled-resume-combined", "controlled_resume")
        row = analysis.analyze_trial(root)
        self.assertEqual(row["status"], "pass")
        self.assertEqual(row["execution_mode"], "controlled_resume")
        self.assertIn("controlled-resume-combined", row["evidence"])

    def test_missing_grade_never_becomes_a_pass_from_green_oracle(self):
        root = self.trial()
        (root / "evidence/audit/grade.json").unlink()
        row = analysis.analyze_trial(root)
        self.assertEqual(row["status"], "not_audited")
        self.assertFalse(row["comparison"]["comparable"])

    def test_selected_final_validation_is_honored(self):
        root = self.trial()
        evidence = root / "evidence"
        write(evidence / "audit-selection.json", {"directory": "audit", "grade_file": "final-validation/grade.json"})
        write(evidence / "audit/final-validation/grade.json", {"status": "incomplete"})
        self.assertEqual(analysis.analyze_trial(root)["status"], "incomplete")

    def test_selected_grade_uses_final_snapshot_oracle_not_base_final_oracle(self):
        root = self.trial()
        evidence = root / "evidence"
        selected_grade = json.loads((evidence / "audit/grade.json").read_text())
        selected_observed = json.loads((evidence / "observed.json").read_text())
        write(evidence / "audit-selection.json", {"directory": "audit", "grade_file": "final-validation/grade.json"})
        write(evidence / "audit/final-validation/grade.json", selected_grade)
        write(evidence / "audit/final-validation/observed.json", selected_observed)
        # This retained base result deliberately disagrees.  It is not bound to
        # the selected final snapshot and must not drive the comparison.
        write(evidence / "final-oracle.json", oracle(blank=False, name=True))
        row = analysis.analyze_trial(root)
        self.assertTrue(row["comparison"]["comparable"])
        self.assertEqual(row["comparison"]["newly_passing"], ["blank"])
        self.assertEqual(row["comparison"]["oracle_sources"]["final"], "snapshot-oracles:b")

    def test_comparison_requires_stable_snapshots_bound_by_selected_grade(self):
        root = self.trial()
        evidence = root / "evidence"
        observed = json.loads((evidence / "observed.json").read_text())
        observed["snapshots"][1]["stable"] = False
        write(evidence / "observed.json", observed)
        write(evidence / "audit/observed.json", observed)
        grade = json.loads((evidence / "audit/grade.json").read_text())
        grade["sequence"]["final_observed_snapshot_stable"] = False
        write(evidence / "audit/grade.json", grade)
        result = analysis.analyze_trial(root)["comparison"]
        self.assertFalse(result["comparable"])
        self.assertEqual(result["newly_passing"], [])
        self.assertIn("final observed snapshot is not stable", " ".join(result["incomplete_reasons"]))

    def test_comparison_requires_selected_audit_snapshot_catalog(self):
        root = self.trial()
        (root / "evidence/audit/observed.json").unlink()
        result = analysis.analyze_trial(root)["comparison"]
        self.assertFalse(result["comparable"])
        self.assertIn("selected grade's observed snapshot catalog is unavailable", result["incomplete_reasons"])

    def test_usage_retains_cached_tokens_without_cost_inference(self):
        evidence = self.root / "evidence"
        evidence.mkdir()
        rows = [{"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 8,
                                                        "output_tokens": 2, "duration_ms": 700}},
                {"type": "item.completed", "usage": {"input_tokens": 999}},
                {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 3, "output_tokens": 0}}]
        (evidence / "events.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{broken")
        result = analysis.token_usage(evidence)
        self.assertEqual(result["observed_receipts"], 2)
        self.assertEqual(result["totals"], {"input_tokens": 15, "cached_input_tokens": 11, "output_tokens": 2})
        self.assertEqual(result["ignored_non_token_usage_keys"], ["duration_ms"])

    def test_cohorts_and_incomplete_trials_are_not_pooled(self):
        result = analysis.summarize([
            {"case_id": "clean_control", "execution_mode": "autonomous", "status": "pass", "review_count": 2},
            {"case_id": "clean_control", "execution_mode": "autonomous", "status": "incomplete"},
            {"case_id": "clean_control", "execution_mode": "controlled_resume", "status": "pass", "review_count": 4},
        ])
        self.assertEqual(result["cohorts"]["autonomous"]["count"], 2)
        self.assertEqual(result["cohorts"]["autonomous"]["statuses"], {"pass": 1, "incomplete": 1})
        self.assertEqual(result["cohorts"]["controlled_resume"]["count"], 1)

    def test_partial_comparisons_do_not_count_as_repairs_or_regressions(self):
        result = analysis.summarize([{
            "case_id": "blank_fallback", "execution_mode": "autonomous", "status": "incomplete",
            "comparison": {"comparable": False, "newly_passing": ["partial-repair"], "regressed": ["partial-loss"]},
        }])
        population = result["cohorts"]["autonomous"]["by_population"]["behavior_repair"]
        self.assertEqual(population["comparable_first_to_final"], 0)
        self.assertEqual(population["runs_with_newly_passing_assertions_after_first_review"], 0)
        self.assertEqual(population["runs_with_regressed_assertions_after_first_review"], 0)

    def test_unprepared_trial_is_visible(self):
        self.assertEqual(analysis.analyze_trial(self.root / "missing")["status"], "not_prepared")

    def test_batch_schedule_retains_unprepared_controlled_roots(self):
        self.trial(self.root / "autonomous/blank/rep-01")
        write(self.root / "batch.json", {"schedule": [
            {"id": "auto-01", "case_id": "blank_fallback", "execution_mode": "autonomous",
             "root": "autonomous/blank/rep-01", "repeat": 1},
            {"id": "controlled-01", "case_id": "clean_control", "execution_mode": "controlled_resume",
             "root": "controlled-resume/rep-01", "repeat": 1},
        ]})
        result = analysis.analyze_batch(self.root)
        self.assertEqual(len(result["trials"]), 2)
        self.assertEqual(result["cohorts"]["controlled_resume"]["statuses"], {"not_prepared": 1})
        self.assertEqual(result["trials"][1]["trial"], "controlled-01")
        self.assertEqual(result["trials"][1]["dispatch_receipts"], [])
        self.assertIsNone(result["trials"][1]["coordinator_terminal_failure"])

    def test_finished_incomplete_dispatch_receipt_marks_unaudited_trial_incomplete(self):
        trial = self.trial(self.root / "opaque-root")
        (trial / "evidence/audit/grade.json").unlink()
        entry = {"id": "trial-01", "case_id": "blank_fallback", "execution_mode": "autonomous",
                 "root": "opaque-root", "repeat": 1}
        write(self.root / "batch.json", {"schedule": [entry]})
        receipts = [
            {"kind": "launched", "id": "trial-01", "root": "opaque-root"},
            {"kind": "finished", "id": "trial-01", "root": "opaque-root", "status": "incomplete",
             "process_failure": "audit", "returncode": 1},
        ]
        attempts = "\n".join(json.dumps(receipt, sort_keys=True) for receipt in receipts) + "\n"
        (self.root / "attempts.jsonl").write_text(attempts)
        result = analysis.analyze_batch(self.root)
        row = result["trials"][0]
        self.assertEqual(row["evidence_status"], "not_audited")
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(row["reasons"], ["coordinator terminal incomplete during audit"])
        self.assertEqual(row["coordinator_terminal_failure"], receipts[-1])
        self.assertEqual(row["dispatch_receipts"], receipts)
        self.assertEqual(result["attempts_sha256"], hashlib.sha256(attempts.encode()).hexdigest())

    def test_seed_failure_is_not_reported_as_post_review_regression(self):
        root = self.trial()
        row = analysis.analyze_trial(root)
        self.assertEqual(row["comparison"]["all_observed_failing_snapshot_ids"], ["a"])
        self.assertEqual(row["comparison"]["observed_post_first_review_failing_snapshot_ids"], [])

    def test_controlled_post_first_failures_are_an_intervention_cohort_observation(self):
        root = self.trial(mode="controlled_resume")
        evidence = root / "evidence"
        observed = json.loads((evidence / "observed.json").read_text())
        observed["snapshots"].append({"id": "c", "stable": True, "candidate_digest": "digest-c",
                                      "manifest": {"formatter.py": {"sha256": "fixed"}}})
        write(evidence / "observed.json", observed)
        write(evidence / "audit/observed.json", observed)
        write(evidence / "snapshot-oracles.json", {
            "a": oracle(blank=True), "b": oracle(blank=False), "c": oracle(blank=True),
        })
        write(evidence / "audit/grade.json", {"status": "pass", "sequence": {
            "states": [
                {"review_id": "first", "snapshot_id": "a", "candidate_digest": "digest-a",
                 "snapshot_stable": True, "completed": True, "classification": "qualifying", "streak_after": 1},
                {"review_id": "reset", "snapshot_id": "b", "candidate_digest": "digest-b",
                 "snapshot_stable": True, "completed": True, "classification": "material", "streak_after": 0},
                {"review_id": "final", "snapshot_id": "c", "candidate_digest": "digest-c",
                 "snapshot_stable": True, "completed": True, "classification": "qualifying", "streak_after": 2},
            ],
            "completed_review_count": 3, "final_observed_snapshot_id": "c",
            "final_observed_candidate_digest": "digest-c", "final_observed_snapshot_stable": True,
            "final_candidate_bound": True,
        }})
        comparison = analysis.analyze_trial(root)["comparison"]
        self.assertEqual(comparison["observed_post_first_review_failing_snapshot_ids"], ["b"])
        self.assertEqual(comparison["post_first_review_failure_cohort"], "evaluator_intervention_controlled_resume")
        self.assertIn("not attributed to self-created", comparison["post_first_review_failure_boundary"])
        self.assertEqual(comparison["regressed"], [])

    def test_controlled_elapsed_requires_both_observed_phases(self):
        root = self.trial(mode="controlled_resume")
        initial = root / "evidence/observed.json"
        data = json.loads(initial.read_text()); data["elapsed_seconds"] = 100
        write(initial, data)
        self.assertIsNone(analysis.analyze_trial(root)["elapsed_seconds"])
        write(root / "controlled-resume-continuation/evidence/observed.json", {"elapsed_seconds": 200})
        self.assertEqual(analysis.analyze_trial(root)["elapsed_seconds"], 300)

    def test_output_cannot_overwrite_an_existing_judgment(self):
        root = self.trial()
        output = root / "evidence/audit/grade.json"
        before = output.read_bytes()
        with mock.patch.object(sys, "argv", ["analysis", "--trials", str(self.root), "--output", str(output)]):
            with self.assertRaises(FileExistsError):
                analysis.main()
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
