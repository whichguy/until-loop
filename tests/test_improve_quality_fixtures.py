#!/usr/bin/env python3
"""Behavioral regressions for the Improve quality-fixture library."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_fixtures as fixtures


class ImproveQualityFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-fixture-tests-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, case_id: str) -> tuple[Path, Path, dict]:
        workspace = self.root / f"{case_id}-workspace"
        evidence = self.root / f"{case_id}-evidence"
        return workspace, evidence, fixtures.prepare_case(case_id, workspace, evidence)

    @staticmethod
    def git(workspace: Path, *arguments: str) -> str:
        return fixtures._git(workspace, *arguments).decode("utf-8", "replace")

    def assert_protected_logically_unchanged(self, workspace: Path, manifest: dict) -> None:
        before = manifest["protected"]
        after = fixtures.protected_state(workspace)
        self.assertEqual(after["staged"], before["staged"])
        self.assertEqual(after["unstaged"], before["unstaged"])
        self.assertEqual(after["untracked"], before["untracked"])
        # A reference repair updates its product path in the index. Git may also
        # refresh private index extensions, so preserve every *out-of-scope*
        # logical index entry rather than comparing the raw index bytes.
        protected_entries = {
            path: entry
            for path, entry in before["index"]["entries"].items()
            if path not in manifest["scope_paths"]
        }
        self.assertEqual(
            {
                path: entry
                for path, entry in after["index"]["entries"].items()
                if path not in manifest["scope_paths"]
            },
            protected_entries,
        )
        self.assertIn("A  user-draft.txt", after["index"]["status_porcelain"])
        self.assertIn(" M notes/in-progress.txt", after["index"]["status_porcelain"])
        self.assertIn("?? scratch.txt", after["index"]["status_porcelain"])

    def test_blank_fallback_seed_is_green_but_oracle_fails_then_reference_passes(self) -> None:
        workspace, evidence, manifest = self.prepare("blank_fallback")

        persisted = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, manifest)
        self.assertFalse((workspace / "manifest.json").exists())
        self.assertEqual(manifest["baseline_commit_count"], 7)
        self.assertEqual(self.git(workspace, "remote"), "")
        self.assertEqual(
            self.git(workspace, "rev-list", "--count", "HEAD").strip(), "7"
        )
        self.assertIn("Use Improve on this repository.", manifest["request"])
        self.assertIn("last seven full Git commit messages", manifest["request"])
        self.assertNotIn("blank", manifest["request"].lower())
        self.assertEqual(fixtures.run_visible_tests(workspace)["passed"], True)

        initial = fixtures.run_oracle("blank_fallback", workspace)
        self.assertFalse(initial["passed"])
        self.assertEqual(
            sorted(name for name, result in initial["assertions"].items() if not result["passed"]),
            manifest["expected_initial_failing_assertions"],
        )
        self.assertTrue(initial["assertions"]["nonblank_unicode_trim"]["passed"])
        unchanged_manifest = (evidence / "manifest.json").read_bytes()

        reference = fixtures.apply_reference("blank_fallback", workspace)
        self.assertEqual(reference["changed_paths"], ["formatter.py", "tests/test_formatter.py"])
        self.assertTrue(reference["commit"])
        self.assertEqual(fixtures.run_visible_tests(workspace)["passed"], True)
        self.assertTrue(fixtures.run_oracle("blank_fallback", workspace)["passed"])
        self.assert_protected_logically_unchanged(workspace, manifest)
        self.assertEqual((evidence / "manifest.json").read_bytes(), unchanged_manifest)

    def test_clean_control_requires_no_reference_change_and_has_adequate_visible_coverage(self) -> None:
        workspace, evidence, manifest = self.prepare("clean_control")

        self.assertEqual(manifest["expected_initial_failing_assertions"], [])
        self.assertTrue(manifest["initial_preflight"]["oracle_passed"])
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("clean_control", workspace)["passed"])
        visible = (workspace / "tests" / "test_formatter.py").read_text(encoding="utf-8")
        self.assertIn("fallback_for_blank_unicode_name", visible)
        head = self.git(workspace, "rev-parse", "HEAD").strip()

        reference = fixtures.apply_reference("clean_control", workspace)
        self.assertEqual(reference, {"case_id": "clean_control", "changed_paths": [], "commit": None, "no_change": True})
        self.assertEqual(self.git(workspace, "rev-parse", "HEAD").strip(), head)
        self.assert_protected_logically_unchanged(workspace, manifest)
        self.assertTrue((evidence / "manifest.json").is_file())

    def test_csv_seed_exposes_independent_quote_diagnostic_and_continuation_failures(self) -> None:
        workspace, evidence, manifest = self.prepare("csv_contract")

        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        initial = fixtures.run_oracle("csv_contract", workspace)
        self.assertFalse(initial["passed"])
        failures = sorted(name for name, result in initial["assertions"].items() if not result["passed"])
        self.assertEqual(failures, manifest["expected_initial_failing_assertions"])
        self.assertTrue(initial["assertions"]["deterministic_result"]["passed"])
        self.assertNotIn("quoted_commas_preserved", (workspace / "tests" / "test_importer.py").read_text(encoding="utf-8"))

        reference = fixtures.apply_reference("csv_contract", workspace)
        self.assertEqual(reference["changed_paths"], ["importer.py", "tests/test_importer.py"])
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        repaired = fixtures.run_oracle("csv_contract", workspace)
        self.assertTrue(repaired["passed"])
        self.assertTrue(all(item["passed"] for item in repaired["assertions"].values()))
        self.assert_protected_logically_unchanged(workspace, manifest)
        self.assertTrue((evidence / "manifest.json").is_file())

    def test_oracle_catches_a_regression_after_the_known_blank_reference_repair(self) -> None:
        workspace, _evidence, _manifest = self.prepare("blank_fallback")
        fixtures.apply_reference("blank_fallback", workspace)
        source = workspace / "formatter.py"
        source.write_text(
            "def format_name(value: str) -> str:\n    return 'Anonymous'\n",
            encoding="utf-8",
        )

        oracle = fixtures.run_oracle("blank_fallback", workspace)
        self.assertFalse(oracle["passed"])
        self.assertFalse(oracle["assertions"]["nonblank_unicode_trim"]["passed"])
        self.assertTrue(oracle["assertions"]["blank_ascii_fallback"]["passed"])

    def test_tenant_cache_seed_hides_cross_tenant_leak_from_one_tenant_visible_tests(self) -> None:
        workspace, _evidence, manifest = self.prepare("tenant_cache")

        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        initial = fixtures.run_oracle("tenant_cache", workspace)
        self.assertFalse(initial["passed"])
        self.assertEqual(
            sorted(name for name, result in initial["assertions"].items() if not result["passed"]),
            manifest["expected_initial_failing_assertions"],
        )
        self.assertTrue(initial["assertions"]["single_tenant_round_trip"]["passed"])
        self.assertNotIn("south", (workspace / "tests" / "test_cache.py").read_text(encoding="utf-8"))

        reference = fixtures.apply_reference("tenant_cache", workspace)
        self.assertEqual(reference["changed_paths"], ["cache.py", "tests/test_cache.py"])
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("tenant_cache", workspace)["passed"])
        self.assert_protected_logically_unchanged(workspace, manifest)

    def test_decline_suggestion_has_correct_current_contract_and_no_reference_change(self) -> None:
        workspace, _evidence, manifest = self.prepare("decline_suggestion")

        suggestion = workspace / "REVIEW_SUGGESTION.md"
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("decline_suggestion", workspace)["passed"])
        self.assertIn("return only\n`value.strip()`", suggestion.read_text(encoding="utf-8"))
        self.assertIn("Anonymous", (workspace / "README.md").read_text(encoding="utf-8"))
        history = self.git(workspace, "log", "--format=%B", "-7")
        self.assertIn("preserve empty formatted names", history)
        self.assertEqual(manifest["review_suggestion"]["path"], "REVIEW_SUGGESTION.md")
        head = self.git(workspace, "rev-parse", "HEAD").strip()

        reference = fixtures.apply_reference("decline_suggestion", workspace)
        self.assertTrue(reference["no_change"])
        self.assertEqual(self.git(workspace, "rev-parse", "HEAD").strip(), head)
        self.assert_protected_logically_unchanged(workspace, manifest)

    def test_commit_retry_requires_a_normal_one_time_hook_retry_and_records_integrity(self) -> None:
        workspace, _evidence, manifest = self.prepare("commit_retry")

        before_hook = manifest["commit_hook"]
        self.assertEqual(before_hook["schedule"], "pending\n")
        self.assertEqual(before_hook["log_lines"], [])
        self.assertIsNone(before_hook["core_hooks_path"])
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertFalse(fixtures.run_oracle("commit_retry", workspace)["passed"])

        reference = fixtures.apply_reference("commit_retry", workspace)
        retry = reference["hook_retry"]
        self.assertNotEqual(retry["first_returncode"], 0)
        self.assertEqual(retry["after_failure"]["log_lines"], ["failed"])
        self.assertEqual(retry["after_retry"]["log_lines"], ["failed", "passed"])
        final_hook = fixtures.commit_hook_state(workspace)
        self.assertEqual(final_hook["hook_sha256"], before_hook["hook_sha256"])
        self.assertEqual(final_hook["hook_mode"], before_hook["hook_mode"])
        self.assertIsNone(final_hook["core_hooks_path"])
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("commit_retry", workspace)["passed"])
        self.assert_protected_logically_unchanged(workspace, manifest)

    def test_weak_skipped_test_is_green_but_does_not_qualify_as_behavioral_coverage(self) -> None:
        workspace, _evidence, manifest = self.prepare("weak_tests")

        initial_visible = fixtures.run_visible_tests(workspace)
        self.assertTrue(initial_visible["passed"])
        self.assertIn("skipped=1", initial_visible["stderr"])
        self.assertFalse(fixtures.run_oracle("weak_tests", workspace)["passed"])
        source = (workspace / "tests" / "test_formatter.py").read_text(encoding="utf-8")
        self.assertIn("@unittest.skip", source)
        self.assertTrue(manifest["visible_suite_boundary"]["initially_skipped"])

        reference = fixtures.apply_reference("weak_tests", workspace)
        self.assertEqual(reference["changed_paths"], ["formatter.py", "tests/test_formatter.py"])
        repaired_visible = fixtures.run_visible_tests(workspace)
        self.assertTrue(repaired_visible["passed"])
        self.assertNotIn("skipped=", repaired_visible["stderr"])
        self.assertTrue(fixtures.run_oracle("weak_tests", workspace)["passed"])
        self.assert_protected_logically_unchanged(workspace, manifest)

    def test_invalid_visible_test_fails_while_product_oracle_passes_then_reference_corrects_only_test(self) -> None:
        workspace, _evidence, manifest = self.prepare("invalid_test")

        self.assertFalse(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("invalid_test", workspace)["passed"])
        self.assertEqual(manifest["expected_initial_failing_assertions"], [])
        source_before = (workspace / "formatter.py").read_bytes()
        test_before = (workspace / "tests" / "test_formatter.py").read_text(encoding="utf-8")
        self.assertIn(", '')", test_before)
        self.assertIn("Anonymous", (workspace / "README.md").read_text(encoding="utf-8"))

        reference = fixtures.apply_reference("invalid_test", workspace)
        self.assertEqual(reference["changed_paths"], ["tests/test_formatter.py"])
        self.assertEqual((workspace / "formatter.py").read_bytes(), source_before)
        self.assertTrue(fixtures.run_visible_tests(workspace)["passed"])
        self.assertTrue(fixtures.run_oracle("invalid_test", workspace)["passed"])
        self.assert_protected_logically_unchanged(workspace, manifest)

    def test_case_manifests_are_json_stable_and_keep_answer_labels_out_of_agent_workspaces(self) -> None:
        expected_cases = {
            "blank_fallback",
            "clean_control",
            "csv_contract",
            "tenant_cache",
            "decline_suggestion",
            "commit_retry",
            "weak_tests",
            "invalid_test",
        }
        self.assertEqual(set(fixtures.CASES), expected_cases)
        for case_id in sorted(expected_cases):
            workspace, evidence, manifest = self.prepare(case_id)
            with self.subTest(case_id=case_id):
                self.assertEqual(
                    json.loads((evidence / "manifest.json").read_text(encoding="utf-8")),
                    manifest,
                )
                agent_text = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in sorted(workspace.rglob("*"))
                    if path.is_file() and ".git" not in path.parts
                )
                for assertion in manifest["expected_initial_failing_assertions"]:
                    self.assertNotIn(assertion, agent_text)
                self.assertNotIn("expected_initial_failing_assertions", manifest["request"])
                self.assertIn("last seven full Git commit messages", manifest["request"])

    def test_test_quality_reference_candidates_execute_tests_and_detect_known_product_regressions(self) -> None:
        for case_id in sorted(fixtures.CASES):
            workspace, _evidence, manifest = self.prepare(case_id)
            with self.subTest(case_id=case_id):
                fixtures.apply_reference(case_id, workspace)
                product = workspace / fixtures._product_module_path(case_id)
                before = product.read_bytes()
                quality = fixtures.run_test_quality(case_id, workspace)
                self.assertEqual(product.read_bytes(), before)
                self.assertTrue(quality["candidate_product_unchanged"])
                self.assertTrue(quality["baseline"]["passed"])
                self.assertGreater(quality["baseline"]["tests_run"], 0)
                self.assertEqual(quality["baseline"]["failures"], [])
                self.assertEqual(quality["baseline"]["errors"], [])
                self.assertEqual(quality["baseline"]["skipped"], [])
                self.assertTrue(quality["mutation_detected"])
                self.assertEqual(quality["mutation_outcome"], "detected")
                self.assertFalse(quality["mutation_inconclusive"])
                self.assertIsNone(quality["mutation_inconclusive_reason"])
                self.assertTrue(quality["mutation"]["failures"])
                self.assertEqual(quality["mutation"]["errors"], [])
                self.assertTrue(quality["passed"])
                self.assertEqual(quality["product_module"], fixtures._product_module_path(case_id))
                self.assertEqual(
                    manifest["product_baseline_digests"].keys(),
                    fixtures._product_digests(workspace, manifest["scope_paths"]).keys(),
                )

    def test_test_quality_reports_skipped_missing_coverage_without_mutation_detection(self) -> None:
        workspace, _evidence, _manifest = self.prepare("weak_tests")

        quality = fixtures.run_test_quality("weak_tests", workspace)
        self.assertTrue(quality["baseline"]["passed"])
        self.assertEqual(len(quality["baseline"]["skipped"]), 1)
        self.assertFalse(quality["mutation_detected"])
        self.assertEqual(quality["mutation_outcome"], "survived")
        self.assertFalse(quality["mutation_inconclusive"])
        self.assertIsNone(quality["mutation_inconclusive_reason"])
        self.assertEqual(quality["mutation"]["failures"], [])
        self.assertEqual(quality["mutation"]["errors"], [])
        self.assertFalse(quality["passed"])

    def test_test_quality_does_not_treat_import_or_syntax_errors_as_detected_regressions(self) -> None:
        workspace, _evidence, _manifest = self.prepare("clean_control")
        with mock.patch.object(fixtures, "_known_defective_product", return_value="def format_name(:\n"):
            quality = fixtures.run_test_quality("clean_control", workspace)

        self.assertTrue(quality["baseline"]["passed"])
        self.assertTrue(quality["mutation"]["errors"])
        self.assertFalse(quality["mutation_detected"])
        self.assertEqual(quality["mutation_outcome"], "inconclusive")
        self.assertTrue(quality["mutation_inconclusive"])
        self.assertEqual(quality["mutation_inconclusive_reason"], "unittest_errors")
        self.assertFalse(quality["passed"])

    def test_test_quality_runs_self_modifying_baseline_only_in_a_disposable_copy(self) -> None:
        workspace, _evidence, _manifest = self.prepare("clean_control")
        product = workspace / "formatter.py"
        unrelated = workspace / "notes" / "in-progress.txt"
        tests = workspace / "tests" / "test_formatter.py"
        product_before = product.read_bytes()
        unrelated_before = unrelated.read_bytes()
        tests_before = tests.read_bytes()
        tests.write_text(
            tests_before.decode("utf-8")
            + "\n\n"
            + "from pathlib import Path\n\n"
            + "class ZProbeMutationTests(unittest.TestCase):\n"
            + "    def test_attempted_write_stays_in_probe(self):\n"
            + "        Path('formatter.py').write_text(\n"
            + "            \"def format_name(value: str) -> str:\\n\"\n"
            + "            \"    normalized = value.strip()\\n\"\n"
            + "            \"    return normalized or 'Anonymous'\\n\",\n"
            + "            encoding='utf-8',\n"
            + "        )\n"
            + "        Path('notes/in-progress.txt').write_text('probe only\\n', encoding='utf-8')\n"
            + "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        tests_after_injection = tests.read_bytes()

        quality = fixtures.run_test_quality("clean_control", workspace)

        self.assertTrue(quality["baseline"]["passed"])
        self.assertEqual(product.read_bytes(), product_before)
        self.assertEqual(unrelated.read_bytes(), unrelated_before)
        self.assertNotEqual(tests_after_injection, tests_before)
        self.assertEqual(tests.read_bytes(), tests_after_injection)
        self.assertTrue(quality["candidate_product_unchanged"])

    def test_test_quality_marks_timeout_mutation_inconclusive(self) -> None:
        workspace, _evidence, _manifest = self.prepare("clean_control")
        baseline = {
            "tests_run": 1,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": True,
            "passed": True,
            "returncode": 0,
            "timed_out": False,
            "error": None,
        }
        timeout = {
            "tests_run": 0,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": False,
            "passed": False,
            "returncode": None,
            "timed_out": True,
            "error": "visible suite timed out",
        }
        with mock.patch.object(fixtures, "_run_unittest_result", side_effect=[baseline, timeout]):
            quality = fixtures.run_test_quality("clean_control", workspace)

        self.assertFalse(quality["mutation_detected"])
        self.assertEqual(quality["mutation_outcome"], "inconclusive")
        self.assertTrue(quality["mutation_inconclusive"])
        self.assertEqual(quality["mutation_inconclusive_reason"], "timeout")
        self.assertFalse(quality["passed"])

    def test_prepare_rejects_existing_or_nested_agent_and_evaluator_paths(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(fixtures.FixtureError, "workspace must be new"):
            fixtures.prepare_case("blank_fallback", existing, self.root / "evidence")

        workspace = self.root / "nested-workspace"
        with self.assertRaisesRegex(fixtures.FixtureError, "disjoint"):
            fixtures.prepare_case("blank_fallback", workspace, workspace / "evidence")

    def test_unknown_case_is_rejected_before_any_workspace_or_evidence_is_created(self) -> None:
        workspace = self.root / "unknown-workspace"
        evidence = self.root / "unknown-evidence"
        with self.assertRaisesRegex(fixtures.FixtureError, "unknown quality fixture ID"):
            fixtures.prepare_case("not-a-case", workspace, evidence)
        self.assertFalse(workspace.exists())
        self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
