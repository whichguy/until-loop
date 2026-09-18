#!/usr/bin/env python3
"""Regression checks for the reusable Improve review-policy boundary."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_preview as preview


SKILL_ROOT = TESTS_DIR.parent
POLICY = Path("examples/improve/references/review-policy.md")
CARD = Path("examples/improve/SKILL.md")
CALLBACK_EVIDENCE = Path("examples/improve/references/callback-evidence.md")
LEGACY_STANDALONE = Path("examples/improve/references/legacy-standalone.md")


class ImproveReviewPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-policy-tests-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def read(relative: Path) -> str:
        return (SKILL_ROOT / relative).read_text(encoding="utf-8")

    def copied_candidate(self, name: str) -> Path:
        candidate = self.root / name
        for relative in preview.selected_source_paths(SKILL_ROOT):
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SKILL_ROOT / relative, target, follow_symlinks=False)
        return candidate

    def test_canonical_reference_has_required_identity_and_owner_entrypoint(self) -> None:
        lines = self.read(POLICY).splitlines()

        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(lines[0], "# Improve review policy")
        self.assertEqual(lines[1], "Policy ID: improve/review-policy/v1")
        policy = " ".join(lines)
        self.assertIn("## Required owner binding", policy)
        self.assertIn("## Owner-managed consumer entrypoint", policy)
        self.assertIn("history window", policy)
        self.assertIn("precise candidate scope", policy)
        self.assertIn("classification rule", policy)
        self.assertIn("evidence locations", policy)
        self.assertIn("commit policy", policy)
        self.assertIn("phase boundary and callback", policy)
        self.assertIn("finalize the whole requested work", policy)
        self.assertIn(
            "If an owner splits a cycle into phases, execute only the assigned phase and return the owner callback.",
            policy,
        )
        self.assertIn("Never run the entire cycle in one action.", policy)

    def test_shared_policy_keeps_complete_review_safeguards_without_host_runtime(self) -> None:
        policy = " ".join(self.read(POLICY).split())

        for heading in (
            "## Review-cycle obligations",
            "## Interrupted work and final inventory",
            "## Independent review",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, policy)
        for required_clause in (
            "Treat reviewer suggestions as findings to triage, not automatic edits.",
            "Do not invent cosmetic edits, speculative rewrites, or redundant",
            "A material finding or material edit resets the clean-review streak to zero",
            "two distinct, consecutive, fully completed",
            "correct an invalid test with an explicit basis",
            "without reapplying a fix or duplicating a commit",
            "initial ownership-aware inventory",
            "unexpected artifacts from tests or tools, including ignored generated",
            "Use a fresh, read-only independent reviewer when available",
            "requested stop, or exhausted budget remains incomplete",
        ):
            with self.subTest(required_clause=required_clause):
                self.assertIn(required_clause, policy)
        lowered = policy.lower()
        for forbidden in ("until-loop", "scripts/", "python3 "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_standalone_card_delegates_shared_policy_and_keeps_its_binding(self) -> None:
        card = " ".join(self.read(CARD).split())

        self.assertIn("[Improve review policy](references/review-policy.md)", card)
        self.assertIn("## Owner-managed consumer entrypoint", card)
        self.assertIn("must not run this standalone card or this card's", card)
        self.assertIn("last seven reachable Git commit messages", card)
        self.assertIn("Trivial classification", card)
        self.assertIn("host-visible task record", card)
        self.assertIn("[callback evidence](references/callback-evidence.md)", card)
        self.assertIn("New runs must not call", card)
        self.assertIn("One callback is one full cycle", card)
        self.assertIn("exact `done_argv`", card)
        self.assertIn("A no-change review gets an honest host record", card)
        self.assertIn("audit-commit-every-iteration request", card)
        self.assertIn(
            "Review, Plan, Changes, Validation, Key learnings, and",
            card,
        )
        self.assertIn("requires one authorized audit record commit", card)
        self.assertIn("## Preview before execution when requested", card)
        self.assertIn("Do not start or resume a run", card)
        self.assertIn("## Execution handoff", card)
        self.assertIn("two consecutive qualifying", card)
        self.assertIn("[the legacy standalone binding]", card)
        self.assertNotIn("## Resolve the few important ambiguities", card)
        self.assertNotIn("## Review-cycle obligations", card)

    def test_callback_evidence_and_legacy_binding_keep_new_and_old_runs_separate(self) -> None:
        callback_evidence = " ".join(self.read(CALLBACK_EVIDENCE).split())
        legacy = " ".join(self.read(LEGACY_STANDALONE).split())

        for required_clause in (
            "does not add a collector, another state file, or an alternative transition authority",
            "host-visible record of one full review cycle",
            "The runtime accepts only a nonblank `evidence` string",
            "Do not call `examples/improve/scripts/capture_evidence.py`",
            "report `unresolved`",
            "previous report is data to recheck",
        ):
            with self.subTest(required_clause=required_clause):
                self.assertIn(required_clause, callback_evidence)
        self.assertIn("explicitly selected standalone Improve run", legacy)
        self.assertIn("It is not an entrypoint for a new Improve request", legacy)
        self.assertIn("`.until-loop/working.md`", legacy)
        self.assertIn("`../scripts/capture_evidence.py`", legacy)
        self.assertIn("The newer `callback-evidence.md` reference is for new runs only", legacy)

    def test_snapshot_includes_the_shared_policy_and_rejects_missing_dependency(self) -> None:
        candidate = self.copied_candidate("candidate")
        selected = preview.selected_source_paths(candidate)

        self.assertIn(POLICY, selected)
        (candidate / POLICY).unlink()
        with self.assertRaisesRegex(preview.ImprovePreviewError, "regular file"):
            preview.selected_source_paths(candidate)

    def test_frozen_snapshot_detects_shared_policy_drift(self) -> None:
        candidate = self.copied_candidate("candidate")
        review_root = self.root / "review"
        snapshot = review_root / "sources" / "policy-dependency"
        source_record = preview.copy_source_snapshot(candidate, snapshot)
        prepared = review_root / "prepared" / "policy-dependency"
        prepared.mkdir(parents=True)
        manifest = {
            "format": preview.PREPARED_FORMAT,
            "source": source_record,
            "cases": [],
        }
        (prepared / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
        (snapshot / POLICY).write_text(
            (snapshot / POLICY).read_text(encoding="utf-8") + "\nDrift.\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(preview.ImprovePreviewError, "snapshot drifted"):
            preview.load_prepared(review_root, "policy-dependency")


if __name__ == "__main__":
    unittest.main()
