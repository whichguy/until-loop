#!/usr/bin/env python3
"""Hermetic checks for the frozen blind discovery and reviewer-value corpus."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_discovery as fixture


class ImproveDiscoveryFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-discovery-tests-")
        self.root = Path(self.temporary.name) / "corpus"
        self.prepared = fixture.prepare(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    @staticmethod
    def packet_schema(packet: str) -> dict[str, object]:
        start = packet.index("```json\n") + len("```json\n")
        end = packet.index("\n```", start)
        return json.loads(packet[start:end])

    def prepare_covered_controls(self, name: str = "covered-controls") -> tuple[Path, dict[str, object]]:
        root = Path(self.temporary.name) / name
        return root, fixture.prepare_covered_controls(root)

    def test_discovery_cases_pass_visible_tests_and_only_flawed_cases_fail_hidden_oracles(self) -> None:
        report = self.prepared["validation"]
        cases = report["discovery_cases"]

        self.assertEqual([item["case_id"] for item in cases], list(fixture.DISCOVERY_ORDER))
        self.assertTrue(all(item["visible"]["returncode"] == 0 for item in cases))
        self.assertTrue(all(item["visible"]["tests_ran"] >= 1 for item in cases))
        hidden_by_id = {item["case_id"]: item["hidden"]["returncode"] for item in cases}
        expected_by_id = {
            spec["id"]: 0 if spec["expected"]["hidden_should_pass"] else 1
            for spec in fixture.DISCOVERY_SPECS
        }
        self.assertEqual(hidden_by_id, expected_by_id)
        self.assertEqual(sum(code == 0 for code in hidden_by_id.values()), 3)
        self.assertEqual(sum(code == 1 for code in hidden_by_id.values()), 3)

    def test_freeze_manifest_detects_any_solver_visible_mutation(self) -> None:
        target = self.root / "solver" / "cases" / "opal-17" / "workspace" / "records.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# temporary test mutation\n", encoding="utf-8")

        with self.assertRaisesRegex(fixture.FixtureError, "public-sha256.json"):
            fixture.validate(self.root)

    def test_preparation_is_byte_stable_and_independent_between_corpora(self) -> None:
        other = Path(self.temporary.name) / "other-corpus"
        fixture.prepare(other)

        self.assertEqual(self.tree_bytes(self.root), self.tree_bytes(other))
        changed = self.root / "solver" / "cases" / "quill-71" / "workspace" / "records.py"
        changed.write_text("raise RuntimeError('isolated temporary mutation')\n", encoding="utf-8")

        report = fixture.validate(other)

        self.assertTrue(report["frozen"])
        self.assertEqual(len(report["discovery_cases"]), 6)

    def test_json_files_are_canonical_and_packets_are_read_only(self) -> None:
        for path in sorted(self.root.rglob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.read_text(encoding="utf-8"), json.dumps(value, indent=2, sort_keys=True) + "\n")

        for case_id in fixture.DISCOVERY_ORDER:
            packet = (self.root / "solver" / "cases" / case_id / "REVIEW_PACKET.md").read_text(encoding="utf-8")
            with self.subTest(case_id=case_id):
                self.assertIn("Do not edit", packet)
                self.assertIn("not an operating-system access control boundary", packet)
                self.assertIn("workspace/records.py", packet)
                self.assertIn("workspace/tests/test_records.py", packet)
                self.assertIn('"findings"', packet)
                self.assertIn('"tests_proposed"', packet)
                self.assertIn('"materiality"', packet)
                self.assertIn('"uncertainty"', packet)
                schema = self.packet_schema(packet)
                self.assertEqual(schema["case_id"], case_id)
                self.assertIn("findings", schema)
                self.assertIn("plan", schema)
                self.assertIn("tests_proposed", schema)

    def test_evaluator_labels_and_pair_mapping_do_not_leak_into_solver_visible_files(self) -> None:
        public_scopes = (self.root / "README.md", self.root / "solver", self.root / "reviewer-value")
        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for scope in public_scopes
            for path in ([scope] if scope.is_file() else sorted(scope.rglob("*")))
            if path.is_file()
        )
        self.assertNotIn("clean-control", public_text)
        self.assertNotIn("requirement-backed-defect", public_text)
        for spec in fixture.DISCOVERY_SPECS:
            for finding in spec["expected"]["findings"]:
                with self.subTest(finding=finding["id"]):
                    self.assertNotIn(finding["id"], public_text)

        evaluator = json.loads(
            (self.root / "evaluator" / "expected-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(evaluator["pair_map"]), {"csv", "config", "state"})

    def test_reviewer_value_baselines_are_unprimed_and_triage_packets_are_simulated(self) -> None:
        expectations = json.loads(
            (self.root / "evaluator" / "reviewer-value-expectations.json").read_text(encoding="utf-8")
        )["reviewer_value"]
        self.assertEqual(set(expectations), {spec["id"] for spec in fixture.REVIEWER_VALUE_SPECS})
        self.assertEqual(
            {item["challenge_kind"] for item in expectations.values()},
            {"real-bug", "redundant-suggestion", "confident-wrong-suggestion"},
        )

        for challenge in fixture.REVIEWER_VALUE_SPECS:
            case_root = self.root / "reviewer-value" / "cases" / challenge["id"]
            baseline = (case_root / "SELF_REVIEW_PACKET.md").read_text(encoding="utf-8")
            triage = (case_root / "TRIAGE_PACKET.md").read_text(encoding="utf-8")
            with self.subTest(case_id=challenge["id"]):
                self.assertNotIn(challenge["suggestion"], baseline)
                self.assertNotIn("Simulated suggestion to triage", baseline)
                self.assertIn(challenge["suggestion"], triage)
                self.assertIn("Origin: `simulated`", triage)
                self.assertIn('"suggestion_triage"', triage)
                self.assertIn('"findings"', triage)
                self.assertIn('"tests_proposed"', triage)
                schema = self.packet_schema(triage)
                self.assertEqual(schema["case_id"], challenge["id"])
                self.assertEqual(schema["suggestion_triage"]["origin"], "simulated")

    def test_covered_controls_have_meaningful_visible_and_oracle_coverage(self) -> None:
        root, prepared = self.prepare_covered_controls()
        report = prepared["validation"]
        cases = report["covered_control_cases"]

        self.assertEqual([item["case_id"] for item in cases], list(fixture.COVERED_CONTROL_ORDER))
        self.assertTrue(all(item["visible"]["returncode"] == 0 for item in cases))
        self.assertTrue(all(item["visible"]["tests_ran"] >= 1 for item in cases))
        self.assertTrue(all(item["oracle"]["returncode"] == 0 for item in cases))
        self.assertFalse((root / "evaluator").exists())

        csv_tests = (root / "solver" / "cases" / "glacier-49" / "workspace" / "tests" / "test_records.py").read_text(encoding="utf-8")
        config_tests = (root / "solver" / "cases" / "spruce-82" / "workspace" / "tests" / "test_records.py").read_text(encoding="utf-8")
        state_tests = (root / "solver" / "cases" / "ember-35" / "workspace" / "tests" / "test_records.py").read_text(encoding="utf-8")
        self.assertIn("empty_reader_record", csv_tests)
        self.assertIn("False", config_tests)
        self.assertIn("replaces_the_whole_record", state_tests)

    def test_covered_controls_freeze_full_policy_and_owner_binding_without_expected_answers(self) -> None:
        root, _ = self.prepare_covered_controls()
        source_policy = fixture.SHARED_REVIEW_POLICY.read_bytes()
        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )
        self.assertFalse((root / "evaluator").exists())
        self.assertNotIn("requirement-backed-defect", public_text)
        self.assertNotIn("clean-control", public_text)
        self.assertNotIn("real-bug", public_text)

        for case_id in fixture.COVERED_CONTROL_ORDER:
            case_root = root / "solver" / "cases" / case_id
            with self.subTest(case_id=case_id):
                self.assertEqual((case_root / "policy" / "REVIEW_POLICY.md").read_bytes(), source_policy)
                binding = (case_root / "OWNER_BINDING.md").read_text(encoding="utf-8")
                self.assertIn("History window: none.", binding)
                self.assertIn("non-Git static fixture", binding)
                self.assertIn("missing required\nregression coverage", binding)
                self.assertIn("Trivial findings are nonsemantic polish only.", binding)
                self.assertIn("Current adequate tests are retained.", binding)
                self.assertIn("An empty plan is valid", binding)
                self.assertIn("no edits, commits, full\nconvergence, standalone adapter", binding)
                packet = (case_root / "REVIEW_PACKET.md").read_text(encoding="utf-8")
                schema = self.packet_schema(packet)
                self.assertEqual(schema["case_id"], case_id)
                self.assertEqual(schema["callback"]["recipient"], "coordinator")
                self.assertEqual(schema["callback"]["phase"], "read_only_review_plan")

    def test_covered_controls_are_byte_stable_and_manifest_isolated(self) -> None:
        root, _ = self.prepare_covered_controls("covered-one")
        other, _ = self.prepare_covered_controls("covered-two")
        self.assertEqual(self.tree_bytes(root), self.tree_bytes(other))

        changed = root / "solver" / "cases" / "spruce-82" / "workspace" / "records.py"
        changed.write_text("raise RuntimeError('isolated temporary mutation')\n", encoding="utf-8")
        with self.assertRaisesRegex(fixture.FixtureError, "public-sha256.json"):
            fixture.validate_covered_controls(root)

        report = fixture.validate_covered_controls(other)
        self.assertTrue(report["frozen"])
        self.assertEqual(len(report["covered_control_cases"]), 3)


if __name__ == "__main__":
    unittest.main()
