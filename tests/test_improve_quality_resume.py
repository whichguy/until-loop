#!/usr/bin/env python3
"""Deterministic checks for the controlled-resume evaluator helper.

These tests use the public v2 CLI to create a pause.  They are protocol-only:
they do not represent an agent having conducted a substantive Improve review.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "until-loop"
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_fixtures as fixtures
import improve_quality_resume as resume
import improve_quality as quality
import improve_quality_grading as grading


class ControlledResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-resume-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "candidate"
        self.evidence = self.root / "evidence"
        self.fixture = fixtures.prepare_case("clean_control", self.workspace, self.evidence)
        self.card = self.root / "frozen-improve" / "SKILL.md"
        self.card.parent.mkdir(parents=True)
        self.card.write_text("frozen test card\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        environment = {"PATH": os.defpath, "LC_ALL": "C", "LANG": "C"}
        return subprocess.run([sys.executable, str(CLI), *args], cwd=self.workspace,
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False, timeout=15)

    def state(self) -> dict:
        return json.loads((self.workspace / ".until-loop/state.json").read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def pause(self, resume_on: str = "condition_observed") -> None:
        contract = {
            "version": 1,
            "policy": "decision-rubric/2",
            "original_request": resume.augmented_request(self.fixture["request"]),
            "interpretation": "Test the public controlled-resume dependency boundary.",
            "criteria": [{
                "id": "C1", "text": "Review the current candidate with current evidence.",
                "basis": {"kind": "request", "reference": "controlled-resume public dependency boundary"},
            }],
        }
        contract_path = self.root / "contract.json"
        self.write_json(contract_path, contract)
        initialized = self.cli("v2", "init", "--repo", str(self.workspace), "--contract-file", str(contract_path))
        self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
        state = self.state()
        action = state["action"]
        assessment = {
            "action_id": action["id"], "contract_revision": state["contract"]["revision"],
            "decision": "blocked", "criteria": [{"id": "C1", "status": "unknown", "evidence": "The public review context is absent."}],
            "next_action": None,
            "blocker": {"reason": "The public review context is absent.",
                        "resumption_condition": "review-context.txt is restored and observed.",
                        "resume_on": resume_on},
        }
        result_path = Path(action["result_path"])
        self.write_json(result_path, assessment)
        submitted = self.cli("v2", "submit", "--repo", str(self.workspace), "--action-id", action["id"])
        self.assertEqual(submitted.returncode, 0, submitted.stderr.decode())
        self.assertEqual(self.state()["phase"], "paused")

    def first_review_record(self) -> None:
        directory = self.workspace / ".until-loop"
        directory.mkdir(exist_ok=True)
        (directory / "working.md").write_text(
            resume.FIRST_REVIEW_HEADING + "\n\n"
            "Scope: current formatter candidate\n"
            "Evidence: visible formatter tests passed\n"
            "Classification: trivial/no-change review\n",
            encoding="utf-8",
        )

    def source_repository(self) -> Path:
        source = self.root / "source"
        source.mkdir()
        quality.command(source, "git", "init", "-q")
        quality.git(source, "config", "user.name", "Fixture")
        quality.git(source, "config", "user.email", "fixture@example.invalid")
        for name in ("SKILL.md", "references/review-policy.md", "runtime/until-loop/ADAPTER.md"):
            path = source / "skills/improve" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen package " + name, encoding="utf-8")
        quality.git(source, "add", "skills")
        quality.git(source, "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "seed")
        return source

    @staticmethod
    def source_markers() -> dict:
        return {"card": True, "policy": True, "adapter": True, "selected_path_observed": True}

    def combined_fixture(self, *, initial_phase: str = "paused", source_reads: dict | None = None,
                         continuation_command: str | None = None, initial_command: str | None = None) -> Path:
        """Build two completed evidence streams without invoking a model."""
        source = self.root / "combined-package"
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text("frozen card\n", encoding="utf-8")
        (source / "references/review-policy.md").write_text("Policy ID: improve/review-policy/v1\n", encoding="utf-8")
        source_data = {"path": str(source), "manifest": quality.tree(source),
                       "digest": quality.digest(quality.canonical(quality.tree(source)))}
        trial = self.root / "combined-trial"
        initial = trial / "evidence"
        continuation = trial / resume.CONTINUATION_DIRNAME / "evidence"
        initial.mkdir(parents=True)
        continuation.mkdir(parents=True)
        # Same basenames under different candidate paths catch lossy ref mapping.
        for relative in ("a/same.txt", "b/same.txt"):
            path = self.workspace / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        first_snapshot = quality.capture(self.workspace, initial, 0, "initial paused boundary")
        second_snapshot = quality.capture(self.workspace, continuation, 0, "injected intermediate boundary")
        quality.write_json(initial / "snapshots.json", [first_snapshot])
        quality.write_json(continuation / "snapshots.json", [second_snapshot])
        provenance = initial / resume.CONTROLLER_DIRNAME / "condition-observed.json"
        provenance.parent.mkdir()
        provenance.write_text(json.dumps({"provenance": {"kind": "condition_observed", "reference": "observed restoration"}}), encoding="utf-8")
        intervention = {"format": resume.FORMAT, "status": "ready_for_resume", "state_history_unchanged": True,
                        "provenance_path": str(provenance), "resume_prompt": "neutral resume"}
        quality.write_json(initial / resume.CONTROLLER_DIRNAME / "intervention.json", intervention)
        markers = self.source_markers() if source_reads is None else source_reads
        first = {"runtime_phase": initial_phase, "invocation_count": 1, "trial_status": "completed",
                 "source_read_evidence": markers, "source_integrity": True}
        second = {"runtime_phase": "done", "invocation_count": 1, "trial_status": "completed",
                  "source_read_evidence": markers, "source_integrity": True}
        quality.write_json(initial / "observed.json", first)
        quality.write_json(continuation / "observed.json", second)
        command = continuation_command or ("cat " + str(provenance))
        initial_event = ({"type": "turn.completed"} if initial_command is None
                         else {"type": "item.started", "item": {"command": initial_command}})
        (initial / "events.jsonl").write_text(json.dumps(initial_event) + "\n", encoding="utf-8")
        (continuation / "events.jsonl").write_text(json.dumps({"type": "item.started", "item": {"command": command}}) + "\n", encoding="utf-8")
        baseline = fixtures.run_oracle("clean_control", self.workspace)
        quality.write_json(initial / "baseline-oracle.json", baseline)
        quality.write_json(continuation / "baseline-oracle.json", baseline)
        root_manifest = {"format": "improve-quality/v1", "case_id": "clean_control", "workspace": str(self.workspace),
                         "evidence": str(initial), "source": source_data, "fixture": self.fixture,
                         "isolation": "deterministic test"}
        continuation_manifest = {"format": "continuation/v1", "case_id": "clean_control", "workspace": str(self.workspace),
                                 "evidence": str(continuation), "source": source_data, "fixture": self.fixture,
                                 "isolation": "deterministic test"}
        quality.write_json(trial / "trial.json", root_manifest)
        quality.write_json(trial / resume.CONTINUATION_DIRNAME / "trial.json", continuation_manifest)
        return trial

    def test_request_augmentation_defines_one_time_public_dependency_boundary(self) -> None:
        text = resume.augmented_request("Improve this candidate.")
        self.assertIn(resume.FIRST_REVIEW_HEADING, text)
        self.assertIn("condition_observed", text)
        self.assertIn("review-context.txt", text)
        self.assertNotIn("this host", text)
        self.assertNotIn("three reviews", text)

    def test_prepare_appends_boundary_before_any_host_invocation(self) -> None:
        root = self.root / "trial"
        manifest = resume.prepare(self.source_repository(), "HEAD", root)
        persisted = json.loads((root / "trial.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["execution_mode"], "controlled_resume")
        self.assertIn(resume.FIRST_REVIEW_HEADING, persisted["fixture"]["request"])
        prompt = (Path(persisted["evidence"]) / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("condition_observed", prompt)
        self.assertIn("one fresh continuation context", prompt)
        self.assertNotIn("there will be no evaluator coaching or follow-up continuation prompt", prompt)
        self.assertFalse((Path(persisted["evidence"]) / "invocation.json").exists())

    def test_start_delegates_to_one_normal_runner_invocation(self) -> None:
        fake = mock.Mock()
        fake.run.return_value = {"invocation_count": 1}
        with mock.patch.object(resume, "_quality", return_value=fake):
            result = resume.start(self.root / "trial", timeout=17, max_commands=23)
        self.assertEqual(result, {"invocation_count": 1})
        fake.run.assert_called_once_with(self.root / "trial", timeout=17, max_commands=23)

    def test_trace_exception_allows_only_exact_provenance_file(self) -> None:
        continuation_evidence = self.root / "continuation-evidence"
        controller_evidence = self.root / "initial-evidence"
        provenance = controller_evidence / "controlled-resume" / "condition-observed.json"
        events = [
            {"type": "item.started", "item": {"command": "cat " + str(provenance)}},
            {"type": "item.started", "item": {"command": "cat " + str(controller_evidence / "manifest.json")}},
        ]
        permitted, forbidden = resume._combined_event_accesses(events, continuation_evidence, provenance)
        self.assertEqual(permitted, [0])
        self.assertEqual(forbidden, [1])

    def test_combine_preserves_full_snapshot_reference_suffixes_and_marks_controlled_mode(self) -> None:
        trial = self.combined_fixture()
        combined = resume.combine(trial)
        observed = combined["observed"]
        self.assertEqual(observed["execution_mode"], "controlled_resume")
        self.assertEqual(observed["invocation_count"], 2)
        self.assertTrue(observed["controlled_resume_verified"])
        paths = [reference for snapshot in observed["snapshots"] for reference in snapshot["relative_evidence_paths"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(any(path.endswith("candidate/a/same.txt") for path in paths))
        self.assertTrue(any(path.endswith("candidate/b/same.txt") for path in paths))
        evidence = Path(combined["combined_root"]) / "evidence"
        self.assertTrue(all((evidence / path).is_file() for path in paths))

    def test_combine_rejects_reference_outside_snapshot(self) -> None:
        trial = self.combined_fixture()
        snapshot = trial / "evidence/snapshots.json"
        value = json.loads(snapshot.read_text(encoding="utf-8"))[0]
        value["relative_evidence_paths"].append("snapshots/s0000/../../outside.txt")
        snapshot.write_text(json.dumps([value]), encoding="utf-8")
        with self.assertRaisesRegex(resume.ResumeControllerError, "escapes"):
            resume.combine(trial)

    def test_combine_requires_paused_initial_state_and_complete_source_markers(self) -> None:
        for label, kwargs in (
            ("not-paused", {"initial_phase": "done"}),
            ("empty-markers", {"source_reads": {}}),
        ):
            with self.subTest(label=label):
                # Each subtest gets a separate workspace root because combine is append-only.
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-resume-sub-")
                self.root = Path(self.temporary.name)
                self.workspace, self.evidence = self.root / "candidate", self.root / "evidence"
                self.fixture = fixtures.prepare_case("clean_control", self.workspace, self.evidence)
                trial = self.combined_fixture(**kwargs)
                self.assertFalse(resume.combine(trial)["observed"]["controlled_resume_verified"])

    def test_combine_marks_broader_evaluator_access_forbidden(self) -> None:
        trial = self.combined_fixture(continuation_command="cat " + str(self.root / "combined-trial/evidence/manifest.json"))
        observed = resume.combine(trial)["observed"]
        self.assertFalse(observed["controlled_resume_verified"])
        self.assertEqual(observed["controlled_resume"]["forbidden_continuation_access_event_ids"], [0])

    def test_combine_retains_initial_access_tripwire_and_frozen_source_hash(self) -> None:
        trial = self.combined_fixture(initial_command="cat " + str(self.root / "combined-trial/evidence/manifest.json"))
        observed = resume.combine(trial)["observed"]
        self.assertFalse(observed["controlled_resume_verified"])
        self.assertFalse(observed["source_integrity"])
        self.assertEqual(observed["controlled_resume"]["initial_forbidden_access_event_ids"], [0])

        # A frozen package changed after preparation cannot be treated as the selected source.
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-resume-drift-")
        self.root = Path(self.temporary.name)
        self.workspace, self.evidence = self.root / "candidate", self.root / "evidence"
        self.fixture = fixtures.prepare_case("clean_control", self.workspace, self.evidence)
        trial = self.combined_fixture()
        policy = self.root / "combined-package/references/review-policy.md"
        policy.write_text("drifted policy\n", encoding="utf-8")
        observed = resume.combine(trial)["observed"]
        self.assertFalse(observed["controlled_resume_verified"])
        self.assertFalse(observed["controlled_resume"]["frozen_source_verified"])

    def test_combined_grader_accepts_granular_refs_and_rejects_invented_ref(self) -> None:
        observed = resume.combine(self.combined_fixture())["observed"]
        self.assertIn("event:0", observed["evidence_refs"])
        snapshot = observed["snapshots"][-1]
        file_ref = snapshot["relative_evidence_paths"][0]
        review = {
            "review_id": "r1", "snapshot_id": snapshot["id"], "classification": "qualifying",
            "evidence_refs": ["event:0", file_ref], "finding_summary": "Captured evidence supports a distinct review.",
            "checks_current": True, "distinct_review": True, "required_commit_satisfied": True,
        }
        judgment = {"auditor_confidence": "supported", "unresolved_material_findings": False,
                    "reviews": [review, {**review, "review_id": "r2", "evidence_refs": ["event:1", file_ref]}]}
        accepted = grading.validate_and_grade(judgment, observed)
        self.assertTrue(accepted["schema_valid"], accepted["errors"])
        invented = {**review, "evidence_refs": ["invented:reference"]}
        rejected = grading.validate_and_grade({**judgment, "reviews": [invented]}, observed)
        self.assertFalse(rejected["schema_valid"])
        self.assertTrue(any("not in observed.evidence_refs" in error for error in rejected["errors"]))

    def test_non_paused_candidate_is_not_exercised_without_writes(self) -> None:
        before = (self.workspace / "formatter.py").read_bytes()
        result = resume.intervene_if_ready(self.workspace, self.evidence, self.fixture, self.card)
        self.assertEqual(result["status"], "not_exercised")
        self.assertIn("state is absent", result["reason"])
        self.assertEqual((self.workspace / "formatter.py").read_bytes(), before)
        self.assertFalse((self.workspace / resume.CONTEXT_FILENAME).exists())
        self.assertFalse((self.evidence / resume.CONTROLLER_DIRNAME).exists())

    def test_wrong_provenance_pause_is_refused_without_intervention(self) -> None:
        self.pause("user_instruction")
        self.first_review_record()
        state_before = (self.workspace / ".until-loop/state.json").read_bytes()
        result = resume.intervene_if_ready(self.workspace, self.evidence, self.fixture, self.card, protocol_only=True)
        self.assertEqual(result["status"], "not_exercised")
        self.assertIn("does not authorize", result["reason"])
        self.assertEqual((self.workspace / ".until-loop/state.json").read_bytes(), state_before)
        self.assertFalse((self.workspace / resume.CONTEXT_FILENAME).exists())

    def test_protocol_only_controller_preserves_state_and_unrelated_work_once(self) -> None:
        self.pause()
        self.first_review_record()
        protected_before = fixtures.protected_state(self.workspace)
        state_before = (self.workspace / ".until-loop/state.json").read_bytes()
        history_before = (self.workspace / ".until-loop/history.jsonl").read_bytes()
        result = resume.intervene_if_ready(self.workspace, self.evidence, self.fixture, self.card, protocol_only=True)
        self.assertEqual(result["status"], "ready_for_resume")
        self.assertEqual(result["mode"], "protocol-only")
        self.assertTrue(result["state_history_unchanged"])
        self.assertEqual((self.workspace / ".until-loop/state.json").read_bytes(), state_before)
        self.assertEqual((self.workspace / ".until-loop/history.jsonl").read_bytes(), history_before)
        self.assertTrue(resume._protected_work_preserved(protected_before, fixtures.protected_state(self.workspace)))
        self.assertEqual((self.workspace / resume.CONTEXT_FILENAME).is_file(), True)
        self.assertNotIn("regression", result["resume_prompt"].lower())
        self.assertIn(str(self.card.resolve()), result["resume_prompt"])
        provenance = Path(result["provenance_path"])
        self.assertEqual(json.loads(provenance.read_text())["provenance"]["kind"], "condition_observed")

        # A wrong provenance cannot resume this condition-observed pause.
        wrong = self.root / "wrong-provenance.json"
        self.write_json(wrong, {"provenance": {"kind": "user_instruction", "reference": "not the observed dependency"}})
        rejected = self.cli("v2", "resume", "--repo", str(self.workspace), "--provenance-file", str(wrong))
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual((self.workspace / ".until-loop/state.json").read_bytes(), state_before)

        head = resume._git(self.workspace, "rev-parse", "HEAD").strip()
        repeated = resume.intervene_if_ready(self.workspace, self.evidence, self.fixture, self.card, protocol_only=True)
        self.assertEqual(repeated["status"], "already_intervened")
        self.assertEqual(resume._git(self.workspace, "rev-parse", "HEAD").strip(), head)


if __name__ == "__main__":
    unittest.main()
