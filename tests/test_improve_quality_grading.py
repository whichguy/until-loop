#!/usr/bin/env python3
"""Adversarial unit tests for independent Improve quality grading."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_grading as grading


class ImproveQualityGradingTests(unittest.TestCase):
    def observed(self, **overrides: object) -> dict:
        evidence_refs = {
            "record:r1": {"path": "records/r1.md"},
            "record:r2": {"path": "records/r2.md"},
            "record:r3": {"path": "records/r3.md"},
            "record:r4": {"path": "records/r4.md"},
            "record:r5": {"path": "records/r5.md"},
            "record:r6": {"path": "records/r6.md"},
            "record:claimed": {"path": "records/claimed-review.md"},
            "record:retry": {"path": "transcript/retry.txt"},
            "record:unknown": {"path": "notes/unknown.md"},
        }
        result = {
            "snapshots": [
                {
                    "id": "before",
                    "candidate_digest": "candidate-before",
                    "relative_evidence_paths": ["snapshots/before.json"],
                },
                {
                    "id": "after",
                    "candidate_digest": "candidate-after",
                    "relative_evidence_paths": ["snapshots/after.json"],
                },
            ],
            "evidence_refs": evidence_refs,
            "runtime_phase": "done",
            "behavior_passed": True,
            "protected_preserved": True,
            "invocation_count": 1,
            "source_integrity": True,
            "trial_status": "complete",
        }
        result.update(overrides)
        return result

    @staticmethod
    def review(
        review_id: str,
        classification: str = "qualifying",
        snapshot_id: str = "after",
        **overrides: object,
    ) -> dict:
        result = {
            "review_id": review_id,
            "snapshot_id": snapshot_id,
            "classification": classification,
            "evidence_refs": [f"record:{review_id}"],
            "finding_summary": f"Evidence-backed {classification} review {review_id}.",
            "checks_current": True,
            "distinct_review": True,
            "required_commit_satisfied": True,
        }
        result.update(overrides)
        return result

    @staticmethod
    def judgment(reviews: list[dict], **overrides: object) -> dict:
        result = {
            "auditor_confidence": "supported",
            "unresolved_material_findings": False,
            "reviews": reviews,
        }
        result.update(overrides)
        return result

    def grade(self, reviews: list[dict], **observed_overrides: object) -> dict:
        return grading.validate_and_grade(self.judgment(reviews), self.observed(**observed_overrides))

    def test_schema_is_small_strict_and_prompt_omits_host_labels(self) -> None:
        schema = grading.JUDGMENT_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"auditor_confidence", "unresolved_material_findings", "reviews"},
        )
        review_schema = schema["properties"]["reviews"]["items"]
        self.assertFalse(review_schema["additionalProperties"])
        self.assertEqual(
            set(review_schema["required"]),
            {
                "review_id",
                "snapshot_id",
                "classification",
                "evidence_refs",
                "finding_summary",
                "checks_current",
                "distinct_review",
                "required_commit_satisfied",
            },
        )

        def assert_supported_transport_schema(value: object) -> None:
            if isinstance(value, dict):
                self.assertNotIn("uniqueItems", value)
                for nested in value.values():
                    assert_supported_transport_schema(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_supported_transport_schema(nested)

        assert_supported_transport_schema(schema)

        observed = self.observed()
        observed["host_streak"] = 99
        observed["expected_fixture_label"] = "seed should be material then clean"
        observed["snapshots"][1].update(
            {
                "stable": False,
                "head": "head-after",
                "desired_count": 4,
                "reference_patch": "evaluator-only-reference-patch",
                "host_streak": 99,
                "state": {"contract": "evaluator-only-contract"},
            }
        )
        prompt = grading.build_judge_prompt(observed)

        self.assertIn("fresh, read-only auditor", prompt)
        self.assertIn("Do not infer a desired review count", prompt)
        self.assertIn("The reviews array contains only\nreview-cycle candidates", prompt)
        self.assertIn("omit a proven callback, retry, diagnostic step", prompt)
        self.assertIn("Do not provide private reasoning", prompt)
        self.assertIn("candidate-after", prompt)
        self.assertIn('"execution_mode": "autonomous"', prompt)
        self.assertIn('"stable": false', prompt)
        self.assertIn('"head": "head-after"', prompt)
        self.assertIn("snapshots/after.json", prompt)
        self.assertIn("cannot\nsupport a qualifying review", prompt)
        self.assertNotIn("host_streak", prompt)
        self.assertNotIn("seed should be material then clean", prompt)
        self.assertNotIn("desired_count", prompt)
        self.assertNotIn("evaluator-only-reference-patch", prompt)
        self.assertNotIn("evaluator-only-contract", prompt)

    def test_two_clean_reviews_on_same_candidate_digest_pass(self) -> None:
        result = self.grade([self.review("r1"), self.review("r2")])

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["sequence"]["qualifying_streak"], 2)
        self.assertEqual(result["sequence"]["review_count"], 2)
        self.assertEqual(result["sequence"]["review_candidate_count"], 2)
        self.assertEqual(result["sequence"]["completed_review_count"], 2)
        self.assertEqual(result["sequence"]["final_two_qualifying_review_ids"], ["r1", "r2"])
        states = result["sequence"]["states"]
        self.assertEqual([state["candidate_digest"] for state in states], ["candidate-after", "candidate-after"])
        self.assertTrue(all(state["qualifies"] for state in states))

    def test_explicitly_unstable_snapshot_cannot_qualify(self) -> None:
        snapshots = copy.deepcopy(self.observed()["snapshots"])
        snapshots[1]["stable"] = False

        result = self.grade([self.review("r1"), self.review("r2")], snapshots=snapshots)

        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["schema_valid"])
        self.assertTrue(
            all(
                "cited snapshot is explicitly unstable" in state["reasons"]
                for state in result["sequence"]["states"]
            )
        )
        self.assertEqual(result["sequence"]["qualifying_streak"], 0)

    def test_non_boolean_snapshot_stability_is_structurally_incomplete(self) -> None:
        snapshots = copy.deepcopy(self.observed()["snapshots"])
        snapshots[1]["stable"] = "not-a-boolean"

        result = self.grade([self.review("r1"), self.review("r2")], snapshots=snapshots)

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["schema_valid"])
        self.assertTrue(any("stable must be a boolean" in error for error in result["errors"]))

    def test_final_qualifying_review_must_bind_the_final_observed_digest(self) -> None:
        snapshots = [
            {
                "id": "candidate-a",
                "candidate_digest": "digest-a",
                "relative_evidence_paths": ["snapshots/a.json"],
                "stable": True,
            },
            {
                "id": "candidate-b",
                "candidate_digest": "digest-b",
                "relative_evidence_paths": ["snapshots/b.json"],
                "stable": True,
            },
        ]
        result = self.grade(
            [
                self.review("r1", snapshot_id="candidate-a"),
                self.review("r2", snapshot_id="candidate-a"),
            ],
            snapshots=snapshots,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["sequence"]["qualifying_streak"], 2)
        self.assertFalse(result["sequence"]["final_candidate_bound"])
        self.assertIn(
            "final qualifying review is not bound to the final observed candidate digest",
            result["incomplete_reasons"],
        )

    def test_final_trivial_change_can_bind_only_the_last_qualifying_review(self) -> None:
        snapshots = [
            {
                "id": "candidate-a",
                "candidate_digest": "digest-a",
                "relative_evidence_paths": ["snapshots/a.json"],
                "stable": True,
            },
            {
                "id": "candidate-b",
                "candidate_digest": "digest-b",
                "relative_evidence_paths": ["snapshots/b.json"],
                "stable": True,
            },
        ]
        result = self.grade(
            [
                self.review("r1", snapshot_id="candidate-a"),
                self.review("r2", snapshot_id="candidate-b"),
            ],
            snapshots=snapshots,
        )

        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["sequence"]["final_candidate_bound"])
        self.assertEqual(
            [state["candidate_digest"] for state in result["sequence"]["states"]],
            ["digest-a", "digest-b"],
        )

    def test_unstable_final_snapshot_cannot_establish_final_candidate_binding(self) -> None:
        snapshots = [
            {
                "id": "candidate-a",
                "candidate_digest": "digest-a",
                "relative_evidence_paths": ["snapshots/a.json"],
                "stable": True,
            },
            {
                "id": "final-capture",
                "candidate_digest": "digest-a",
                "relative_evidence_paths": ["snapshots/final.json"],
                "stable": False,
            },
        ]
        result = self.grade(
            [
                self.review("r1", snapshot_id="candidate-a"),
                self.review("r2", snapshot_id="candidate-a"),
            ],
            snapshots=snapshots,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["sequence"]["final_candidate_bound"])
        self.assertIn(
            "final observed snapshot is explicitly unstable",
            result["incomplete_reasons"],
        )

    def test_material_then_two_clean_reviews_passes_in_three_reviews(self) -> None:
        result = self.grade(
            [
                self.review("r1", "material", "before"),
                self.review("r2"),
                self.review("r3"),
            ]
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["sequence"]["last_material_review_id"], "r1")
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [0, 1, 2],
        )

    def test_two_material_reviews_then_two_clean_reviews_passes_in_four(self) -> None:
        result = self.grade(
            [
                self.review("r1", "material", "before"),
                self.review("r2", "material", "after"),
                self.review("r3"),
                self.review("r4"),
            ]
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [0, 0, 1, 2],
        )
        self.assertEqual(result["sequence"]["last_material_review_id"], "r2")

    def test_later_material_review_resets_recomputed_streak(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review("r2", "material"),
                self.review("r3"),
                self.review("r4"),
            ],
            host_streak=999,
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [1, 0, 1, 2],
        )
        self.assertEqual(result["sequence"]["last_material_review_id"], "r2")

    def test_more_than_four_legitimate_clean_reviews_remains_a_pass(self) -> None:
        result = self.grade([self.review(f"r{number}") for number in range(1, 6)])

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["sequence"]["review_count"], 5)
        self.assertEqual(result["sequence"]["qualifying_streak"], 5)

    def test_controlled_resume_is_separate_and_accepts_two_verified_invocations(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review("r2", "material"),
                self.review("r3"),
                self.review("r4"),
            ],
            execution_mode="controlled_resume",
            invocation_count=2,
            controlled_resume_verified=True,
        )

        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["outcome"]["autonomous"])
        self.assertTrue(result["outcome"]["invocation_protocol_satisfied"])
        self.assertIn(
            "controller-assisted and must be reported separately from autonomous trials",
            " ".join(result["boundaries"]),
        )

    def test_two_invocations_without_controlled_mode_remain_incomplete(self) -> None:
        result = self.grade([self.review("r1"), self.review("r2")], invocation_count=2)

        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["outcome"]["autonomous"])
        self.assertFalse(result["outcome"]["invocation_protocol_satisfied"])
        self.assertIn(
            "autonomous trial was not established from exactly one Improve invocation",
            result["incomplete_reasons"],
        )

    def test_controlled_resume_requires_verified_controller_receipt(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review("r2", "material"),
                self.review("r3"),
                self.review("r4"),
            ],
            execution_mode="controlled_resume",
            invocation_count=2,
            controlled_resume_verified=False,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["schema_valid"])
        self.assertFalse(result["outcome"]["invocation_protocol_satisfied"])
        self.assertIn(
            "controlled resume lacks an evaluator-owned verified controller receipt",
            result["incomplete_reasons"],
        )

    def test_controlled_resume_rejects_the_wrong_invocation_count(self) -> None:
        result = self.grade(
            [self.review("r1"), self.review("r2")],
            execution_mode="controlled_resume",
            invocation_count=1,
            controlled_resume_verified=True,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["outcome"]["invocation_protocol_satisfied"])
        self.assertIn(
            "controlled resume requires exactly two Improve invocations",
            result["incomplete_reasons"],
        )

    def test_execution_mode_and_controller_receipt_types_are_strict(self) -> None:
        variants = {
            "mode-bool": {"execution_mode": True},
            "unknown-mode": {"execution_mode": "extra_invocations"},
            "receipt-string": {
                "execution_mode": "controlled_resume",
                "invocation_count": 2,
                "controlled_resume_verified": "yes",
            },
        }
        for label, overrides in variants.items():
            with self.subTest(label=label):
                result = self.grade([self.review("r1"), self.review("r2")], **overrides)
                self.assertEqual(result["status"], "incomplete")
                self.assertFalse(result["schema_valid"])
                self.assertTrue(result["errors"])

    def test_controlled_prompt_retains_actual_count_and_separate_boundary(self) -> None:
        prompt = grading.build_judge_prompt(
            self.observed(
                execution_mode="controlled_resume",
                invocation_count=2,
                controlled_resume_verified=True,
            )
        )

        self.assertIn('"invocation_count": 2', prompt)
        self.assertIn('"execution_mode": "controlled_resume"', prompt)
        self.assertIn("not an autonomous trial", prompt)

    def test_unsupported_claimed_review_resets_the_streak(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review(
                    "claimed",
                    distinct_review=False,
                    finding_summary="Claimed review has no distinct-review evidence.",
                ),
                self.review("r2"),
            ]
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [1, 0, 1],
        )
        claimed_state = result["sequence"]["states"][1]
        self.assertFalse(claimed_state["completed"])
        self.assertFalse(claimed_state["qualifies"])
        self.assertIn("review is not evidenced as distinct", claimed_state["reasons"])
        self.assertEqual(result["sequence"]["review_count"], 3)
        self.assertEqual(result["sequence"]["completed_review_count"], 2)

    def test_proven_retry_reference_omitted_from_reviews_preserves_clean_convergence(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review(
                    "r2",
                    evidence_refs=["record:r2", "record:retry"],
                    finding_summary="A cited retry was transport-only; the completed review is distinct and current.",
                ),
            ]
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["sequence"]["review_count"], 2)
        self.assertEqual(result["sequence"]["review_candidate_count"], 2)
        self.assertEqual(result["sequence"]["completed_review_count"], 2)
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [1, 2],
        )

    def test_stale_checks_cannot_advance_the_streak(self) -> None:
        result = self.grade(
            [
                self.review("r1", checks_current=False),
                self.review("r2"),
            ]
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            [state["streak_after"] for state in result["sequence"]["states"]],
            [0, 1],
        )
        self.assertIn("checks are not current", result["sequence"]["states"][0]["reasons"])

    def test_unknown_record_breaks_sequence_and_remains_incomplete(self) -> None:
        result = self.grade(
            [
                self.review("r1"),
                self.review("unknown", "unknown"),
                self.review("r2"),
            ]
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["sequence"]["qualifying_streak"], 1)
        self.assertEqual(
            result["sequence"]["incomplete_or_unknown_review_ids"],
            ["unknown"],
        )

    def test_duplicate_review_ids_are_rejected(self) -> None:
        first = self.review("r1")
        duplicate = copy.deepcopy(first)
        duplicate["finding_summary"] = "A separate claim with an invalid duplicate identifier."

        result = grading.validate_and_grade(
            self.judgment([first, duplicate]),
            self.observed(),
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["schema_valid"])
        self.assertTrue(any("duplicate review_id" in error for error in result["errors"]))

    def test_invented_snapshot_digest_and_evidence_references_are_rejected(self) -> None:
        variants = {
            "snapshot": self.review("r1", snapshot_id="invented-snapshot"),
            "digest": {
                **self.review("r1"),
                "candidate_digest": "invented-digest",
            },
            "evidence": self.review("r1", evidence_refs=["invented:evidence"]),
        }
        for label, review in variants.items():
            with self.subTest(label=label):
                result = grading.validate_and_grade(
                    self.judgment([review]),
                    self.observed(),
                )
                self.assertEqual(result["status"], "incomplete")
                self.assertFalse(result["schema_valid"])
                self.assertTrue(result["errors"])

    def test_evidence_less_distinct_assertion_and_empty_explanation_are_rejected(self) -> None:
        for label, review in {
            "no-evidence": self.review("r1", evidence_refs=[]),
            "duplicate-evidence": self.review("r1", evidence_refs=["record:r1", "record:r1"]),
            "empty-summary": self.review("r1", finding_summary="   "),
        }.items():
            with self.subTest(label=label):
                result = grading.validate_and_grade(self.judgment([review]), self.observed())
                self.assertEqual(result["status"], "incomplete")
                self.assertFalse(result["schema_valid"])
                self.assertTrue(result["errors"])

    def test_boolean_invocation_count_is_not_accepted_as_an_integer(self) -> None:
        result = self.grade(
            [self.review("r1"), self.review("r2")],
            invocation_count=True,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["schema_valid"])
        self.assertTrue(any("not a boolean" in error for error in result["errors"]))

    def test_missed_material_issue_with_behavior_failure_is_a_fail(self) -> None:
        result = self.grade(
            [self.review("r1"), self.review("r2")],
            behavior_passed=False,
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["schema_valid"])
        self.assertIn("independent behavioral acceptance evidence failed", result["fail_reasons"])

    def test_limited_confidence_or_unresolved_material_finding_cannot_pass(self) -> None:
        for label, overrides in {
            "limited": {"auditor_confidence": "limited"},
            "unresolved": {"unresolved_material_findings": True},
        }.items():
            with self.subTest(label=label):
                result = grading.validate_and_grade(
                    self.judgment([self.review("r1"), self.review("r2")], **overrides),
                    self.observed(),
                )
                self.assertEqual(result["status"], "incomplete")
                self.assertTrue(result["schema_valid"])
                self.assertTrue(result["incomplete_reasons"])

    def test_preservation_source_and_terminal_facts_gate_a_strong_outcome(self) -> None:
        variants = {
            "preservation": {"protected_preserved": False},
            "source": {"source_integrity": False},
            "nonterminal": {"runtime_phase": "review", "trial_status": "incomplete"},
            "duplicate-invocation": {"invocation_count": 2},
        }
        for label, overrides in variants.items():
            with self.subTest(label=label):
                result = self.grade([self.review("r1"), self.review("r2")], **overrides)
                expected = "fail" if label in {"preservation", "source"} else "incomplete"
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["schema_valid"])

    def test_boundaries_do_not_claim_semantic_proof(self) -> None:
        result = self.grade([self.review("r1"), self.review("r2")])

        self.assertEqual(result["status"], "pass")
        boundaries = " ".join(result["boundaries"]).lower()
        self.assertIn("cannot prove", boundaries)
        self.assertIn("semantic classification", boundaries)


if __name__ == "__main__":
    unittest.main()
