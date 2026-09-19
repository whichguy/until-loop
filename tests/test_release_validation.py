#!/usr/bin/env python3
"""Hermetic checks for the opt-in callback release-validation controller.

The small runtime below only supplies deterministic packets for controller
tests.  No test starts a model; live read-only interpretation probes are an
explicit ``run-nl`` command.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import release_validation as release


RUNTIME = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

def packet(contract, state, action):
    return {
        "status": "active", "state_file": str(state), "workspace": contract["workspace"],
        "work": contract["work"], "conditions": {"exit": contract["exit_condition"], "repeat": contract["repeat_condition"]},
        "progress": {"action_number": action, "trivial_streak": 0, "required_trivial_reviews": contract["required_trivial_reviews"]},
        "context": contract["context"], "last_report": None, "instruction": "one full callback cycle",
        "next_argv": [sys.executable, __file__, "next", "--state", str(state)],
        "done_argv": [sys.executable, __file__, "done", "--state", str(state), "--action=fixture:" + str(action)],
        "report_schema": {"type": "object"},
    }

args = sys.argv[1:]
if args and args[0] == "start":
    directory = Path(args[args.index("--directory") + 1])
    state = directory / "fixture-state.json"
    state.write_text("fixture state", encoding="utf-8")
    print(json.dumps(packet(json.load(sys.stdin), state, 1)))
else:
    raise SystemExit("fixture runtime only supports start")
'''


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="release-validation-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self._source_repository()

    def _source_repository(self) -> None:
        package = self.source / "skills" / "improve"
        (package / "references").mkdir(parents=True)
        (package / "runtime" / "until-loop" / "scripts").mkdir(parents=True)
        (package / "SKILL.md").write_text("fixture Improve card\n", encoding="utf-8")
        (package / "references" / "review-policy.md").write_text("fixture policy\n", encoding="utf-8")
        (package / "runtime" / "until-loop" / "ADAPTER.md").write_text("fixture adapter\n", encoding="utf-8")
        script = package / release.RUNTIME_RELATIVE
        script.write_text(RUNTIME, encoding="utf-8")
        script.chmod(0o755)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "skills")
        self.git("-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture package")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.source), *arguments], capture_output=True, check=True)
        return result.stdout.decode("utf-8", "replace")

    def prepare(self, case: str = "clean", name: str = "trial") -> Path:
        root = self.base / name
        result = release.prepare(self.source, "HEAD", root, case)
        self.assertTrue(Path(result["packet"]).is_file())
        self.assertTrue(Path(result["prompt"]).is_file())
        self.assertFalse((root / "candidate" / ".until-loop").exists())
        return root

    @staticmethod
    def manifest(root: Path) -> dict:
        return json.loads((root / "trial.json").read_text(encoding="utf-8"))

    def receipt(self, root: Path, *, terminal: bool = False, classification: str = "trivial") -> Path:
        manifest = self.manifest(root)
        previous = json.loads((root / manifest["current_packet"]["path"]).read_text(encoding="utf-8"))
        state = Path(previous["state_file"])
        report = {
            "classification": classification, "exit_assessment": "satisfied" if terminal else "unsatisfied",
            "continuation_assessment": "allowed", "evidence": "Actual synthetic cycle observations.",
            "handoff": "Current candidate and records are available to the next fresh executor.",
        }
        if terminal:
            state.unlink()
            status, action, next_argv, done_argv, schema = "complete", previous["progress"]["action_number"], None, None, None
        else:
            status, action = "active", previous["progress"]["action_number"] + 1
            next_argv = [sys.executable, "fixture", "next", "--state", str(state)]
            done_argv = [sys.executable, "fixture", "done", "--state", str(state), "--action=fixture:" + str(action)]
            schema = {"type": "object"}
        value = {
            "status": status, "state_file": str(state), "workspace": previous["workspace"], "work": previous["work"],
            "conditions": previous["conditions"],
            "progress": {"action_number": action, "trivial_streak": previous["progress"]["trivial_streak"] + 1 if classification == "trivial" else 0, "required_trivial_reviews": 2},
            "context": previous["context"], "last_report": report, "instruction": "returned runtime packet",
            "next_argv": next_argv, "done_argv": done_argv, "report_schema": schema,
        }
        path = self.base / f"receipt-{len(manifest['checkpoints']) + 1}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def transcript(self, name: str = "transcript.txt") -> Path:
        path = self.base / name
        path.write_text("history: seven commits read\nreview: scoped candidate inspected\ncheck: command completed\n", encoding="utf-8")
        return path

    def checkpoint(self, root: Path, *, terminal: bool = False, classification: str = "trivial") -> dict:
        return release.checkpoint(root, self.receipt(root, terminal=terminal, classification=classification), self.transcript())

    def test_prepare_freezes_before_fixture_and_writes_one_cycle_packet_without_model(self) -> None:
        with mock.patch.object(release.fresh_context, "run_host") as host:
            root = self.prepare("repair")
        manifest = self.manifest(root)
        freeze = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
        self.assertEqual(freeze["package"]["revision"], manifest["package"]["revision"])
        self.assertEqual(freeze["package"]["digest"], manifest["package"]["digest"])
        prompt = Path(release.packet(root)["prompt"]).read_text(encoding="utf-8")
        self.assertIn("one full callback cycle", prompt)
        self.assertIn("stop this executor", prompt)
        self.assertIn("Do not push", prompt)
        self.assertNotIn("blank_ascii_fallback", prompt)
        host.assert_not_called()

    def test_missing_or_malformed_receipt_is_retained_as_incomplete(self) -> None:
        for label, receipt in (("missing", self.base / "missing.json"), ("malformed", self.base / "malformed.json")):
            with self.subTest(label=label):
                root = self.prepare(name="trial-" + label)
                if label == "malformed":
                    receipt.write_text("{bad", encoding="utf-8")
                observed = release.checkpoint(root, receipt, self.transcript(label + ".txt"))
                self.assertEqual(observed["status"], "incomplete")
                self.assertTrue(observed["protocol"]["errors"])
                if label == "malformed":
                    self.assertEqual((root / "evaluator/cycles/c001/a001/callback.raw.json").read_text(encoding="utf-8"), "{bad")

    def test_missing_transcript_is_incomplete_even_with_an_active_packet(self) -> None:
        root = self.prepare()
        observed = release.checkpoint(root, self.receipt(root), None)
        self.assertEqual(observed["status"], "incomplete")
        self.assertFalse(observed["protocol"]["transcript_captured"])
        self.assertEqual(observed["semantic_review"]["status"], "unassessed")

    def test_valid_active_then_terminal_chain_keeps_semantics_unassessed(self) -> None:
        root = self.prepare()
        active = self.checkpoint(root)
        self.assertEqual(active["status"], "recorded")
        self.assertTrue(active["protocol"]["chain_valid"])
        self.assertTrue(active["outcomes"]["snapshot"]["stable"])
        self.assertEqual(active["semantic_review"]["status"], "unassessed")
        terminal = self.checkpoint(root, terminal=True)
        self.assertEqual(terminal["status"], "recorded")
        self.assertTrue(terminal["protocol"]["chain_valid"])
        self.assertEqual(self.manifest(root)["current_packet"]["status"], "complete")

    def test_terminal_packet_does_not_create_executor_material(self) -> None:
        root = self.prepare()
        self.checkpoint(root)
        self.checkpoint(root, terminal=True)
        transport = root / "transport"
        before = sorted(path.relative_to(root) for path in transport.rglob("*"))

        observed = release.packet(root)

        after = sorted(path.relative_to(root) for path in transport.rglob("*"))
        self.assertEqual(observed["status"], "complete")
        self.assertTrue(Path(observed["packet"]).is_file())
        self.assertIsNone(observed["directory"])
        self.assertIsNone(observed["prompt"])
        self.assertIsNone(observed["receipt"])
        self.assertIsNone(observed["transcript"])
        self.assertEqual(after, before)

    def test_controller_consumes_real_runtime_callbacks_through_material_reset_and_completion(self) -> None:
        script = self.source / "skills/improve" / release.RUNTIME_RELATIVE
        script.write_bytes((TESTS_DIR.parent / "scripts/until_loop_ephemeral.py").read_bytes())
        self.git("add", "skills")
        self.git("-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "-qm", "bind real ephemeral runtime")
        root = self.prepare()
        state_file = None
        for number, (classification, exit_assessment, status, streak) in enumerate([
            ("trivial", "unsatisfied", "active", 1),
            ("non-trivial", "unsatisfied", "active", 0),
            ("trivial", "unsatisfied", "active", 1),
            ("trivial", "satisfied", "complete", 2),
        ], start=1):
            with self.subTest(cycle=number):
                manifest = self.manifest(root)
                current = json.loads((root / manifest["current_packet"]["path"]).read_bytes())
                state_file = Path(current["state_file"])
                report = {
                    "classification": classification, "exit_assessment": exit_assessment,
                    "continuation_assessment": "allowed",
                    "evidence": "Synthetic controller/runtime integration input, not a model review.",
                    "handoff": "Protocol fixture with protected candidate work; no semantic convergence claim.",
                }
                result = subprocess.run(current["done_argv"], input=json.dumps(report).encode(),
                                        capture_output=True, cwd=self.base, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                receipt = self.base / f"real-callback-{number}.json"
                receipt.write_bytes(result.stdout)
                observed = release.checkpoint(root, receipt, self.transcript())
                self.assertEqual(observed["status"], "recorded")
                self.assertTrue(observed["protocol"]["chain_valid"])
                self.assertEqual(observed["semantic_review"]["status"], "unassessed")
                packet = json.loads(result.stdout)
                self.assertEqual(packet["status"], status)
                self.assertEqual(packet["progress"]["trivial_streak"], streak)
        self.assertFalse(state_file.exists())
        self.assertIsNone(release.packet(root)["prompt"])

    def test_tampered_package_never_counts_as_a_valid_checkpoint(self) -> None:
        root = self.prepare()
        (root / "package" / "SKILL.md").write_text("tampered\n", encoding="utf-8")
        observed = self.checkpoint(root)
        self.assertEqual(observed["status"], "incomplete")
        self.assertFalse(observed["outcomes"]["package"]["passed"])
        self.assertIn("frozen package changed", observed["protocol"]["errors"])

    def test_protected_user_work_is_checked_separately_from_callback_protocol(self) -> None:
        root = self.prepare()
        candidate = root / "candidate"
        (candidate / "user-draft.txt").write_text("overwritten\n", encoding="utf-8")
        observed = self.checkpoint(root)
        self.assertEqual(observed["status"], "recorded")
        self.assertFalse(observed["outcomes"]["protected_work"]["passed"])
        self.assertEqual(observed["semantic_review"]["status"], "unassessed")

    def test_regression_requires_assessed_qualifying_review_then_preserves_controls(self) -> None:
        root = self.prepare("regression")
        with self.assertRaisesRegex(ValueError, "independently assessed"):
            release.inject_regression(root)
        self.checkpoint(root)
        manifest = self.manifest(root)
        manifest["checkpoints"][0].pop("callback", None)
        (root / "trial.json").write_text(json.dumps(manifest), encoding="utf-8")
        assessment = self.base / "assessment.json"
        payload = {
            "format": release.ASSESSMENT_FORMAT,
            "reviews": [{"cycle": 1, "judgment": "qualifying", "basis": "Independent assessor read the retained transcript and records."}],
            "unresolved_material_findings": True,
        }
        assessment.write_text(json.dumps(payload), encoding="utf-8")
        recorded = release.assess(root, assessment)
        self.assertEqual(recorded["first_qualifying_cycle"], 1)
        with self.assertRaisesRegex(ValueError, "no unresolved material"):
            release.inject_regression(root)
        payload["unresolved_material_findings"] = False
        assessment.write_text(json.dumps(payload), encoding="utf-8")
        release.assess(root, assessment)
        injected = release.inject_regression(root)
        self.assertTrue(injected["passed"])
        self.assertFalse(injected["oracle"]["passed"])
        self.assertTrue(injected["protected_work_unchanged"])
        self.assertTrue(injected["runtime_state_unchanged"])
        self.assertTrue(injected["prior_packet_unchanged"])
        self.assertEqual(injected["head"]["before"], injected["head"]["after"])

    def test_assessments_are_append_only_cover_every_cycle_and_cannot_rewrite_judgments(self) -> None:
        root = self.prepare()
        self.checkpoint(root)
        self.checkpoint(root, terminal=True)
        assessment = self.base / "assessment.json"
        def write(rows: list[dict]) -> None:
            assessment.write_text(json.dumps({
                "format": release.ASSESSMENT_FORMAT, "reviews": rows,
                "unresolved_material_findings": False,
            }), encoding="utf-8")
        write([{"cycle": 1, "judgment": "incomplete", "basis": "The first retained record needs independent completion review."}])
        with self.assertRaisesRegex(ValueError, "cover every recorded cycle"):
            release.assess(root, assessment)
        rows = [
            {"cycle": 1, "judgment": "incomplete", "basis": "The first retained record needs independent completion review."},
            {"cycle": 2, "judgment": "qualifying", "basis": "The terminal record has an independently supported clean review."},
        ]
        write(rows)
        release.assess(root, assessment)
        release.assess(root, assessment)
        self.assertTrue((root / "evaluator/assessments/a001.raw.json").is_file())
        self.assertTrue((root / "evaluator/assessments/a002.json").is_file())
        self.assertTrue((root / "evaluator/assessments/a003.json").is_file())
        rows[0] = {"cycle": 1, "judgment": "qualifying", "basis": "Attempt to rewrite prior judgment."}
        write(rows)
        with self.assertRaisesRegex(ValueError, "cannot rewrite"):
            release.assess(root, assessment)
        rows[0] = {"cycle": 1, "judgment": "incomplete", "basis": "The first retained record needs independent completion review."}
        write(rows)
        release.assess(root, assessment)
        self.assertTrue((root / "evaluator/assessments/a004.raw.json").is_file())
        self.assertTrue((root / "evaluator/assessments/a005.json").is_file())

    def test_tampered_historical_callback_after_a_later_cycle_is_rejected_by_digest(self) -> None:
        root = self.prepare()
        self.checkpoint(root)
        self.checkpoint(root, terminal=True)
        manifest = self.manifest(root)
        first = root / manifest["checkpoints"][0]["path"]
        callback = first.parent / "callback.raw.json"
        callback.write_bytes(callback.read_bytes() + b"\n")
        assessment = self.base / "digest-assessment.json"
        assessment.write_text(json.dumps({
            "format": release.ASSESSMENT_FORMAT,
            "reviews": [
                {"cycle": 1, "judgment": "qualifying", "basis": "First review."},
                {"cycle": 2, "judgment": "qualifying", "basis": "Second review."},
            ], "unresolved_material_findings": False,
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest does not match retained bytes"):
            release.assess(root, assessment)

    def test_regression_rejects_a_later_qualifying_review_and_snapshot_ordinals_do_not_collide(self) -> None:
        root = self.prepare("regression", "regression-late")
        self.checkpoint(root)
        self.checkpoint(root)
        assessment = self.base / "late-assessment.json"
        assessment.write_text(json.dumps({
            "format": release.ASSESSMENT_FORMAT,
            "reviews": [
                {"cycle": 1, "judgment": "qualifying", "basis": "First independently assessed clean review."},
                {"cycle": 2, "judgment": "qualifying", "basis": "Later independently assessed clean review."},
            ], "unresolved_material_findings": False,
        }), encoding="utf-8")
        release.assess(root, assessment)
        with self.assertRaisesRegex(ValueError, "latest independently assessed first"):
            release.inject_regression(root)

        root = self.prepare("regression", "regression-snapshots")
        self.checkpoint(root)
        assessment.write_text(json.dumps({
            "format": release.ASSESSMENT_FORMAT,
            "reviews": [{"cycle": 1, "judgment": "qualifying", "basis": "First independently assessed clean review."}],
            "unresolved_material_findings": False,
        }), encoding="utf-8")
        release.assess(root, assessment)
        release.inject_regression(root)
        (root / "candidate/formatter.py").write_text(
            "def format_name(value: str) -> str:\n    \"\"\"Trim a display name and return its documented fallback.\"\"\"\n    return value.strip() or 'Anonymous'\n",
            encoding="utf-8",
        )
        observed = self.checkpoint(root, classification="non-trivial")
        self.assertTrue(observed["outcomes"]["snapshot"]["stable"])
        self.assertEqual(observed["outcomes"]["snapshot"]["id"], "s0003")

    def test_regression_rejects_stale_assessment_before_mutating_the_candidate(self) -> None:
        root = self.prepare("regression")
        self.checkpoint(root)
        assessment = self.base / "assessment.json"
        assessment.write_text(json.dumps({
            "format": release.ASSESSMENT_FORMAT,
            "reviews": [{"cycle": 1, "judgment": "qualifying", "basis": "First independently assessed review."}],
            "unresolved_material_findings": False,
        }), encoding="utf-8")
        release.assess(root, assessment)
        self.checkpoint(root, classification="non-trivial")
        manifest = self.manifest(root)
        current = json.loads((root / manifest["current_packet"]["path"]).read_text(encoding="utf-8"))
        protected = [root / "trial.json", root / "candidate/formatter.py",
                     root / "evaluator/snapshots.json", Path(current["state_file"])]
        before = {path: path.read_bytes() for path in protected}

        with self.assertRaisesRegex(ValueError, "complete current assessment"):
            release.inject_regression(root)

        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertFalse((root / "evaluator/intervention.json").exists())

    def test_nl_preparation_is_no_model_and_optional_run_uses_fresh_host_per_case(self) -> None:
        root = self.prepare()
        with mock.patch.object(release.fresh_context, "run_host", return_value={"returncode": 0}) as host:
            prepared = release.prepare_nl(root)
            self.assertEqual(prepared["case_count"], 6)
            self.assertTrue(Path(prepared["prepared"]).is_file())
            host.assert_not_called()
            results = release.run_nl(root, "read-only")
        self.assertEqual(len(results), 6)
        self.assertEqual(host.call_count, 6)
        prompt, schema = host.call_args.kwargs["prompt_spec"]
        self.assertIn("contract_json", prompt)
        self.assertEqual(schema, release.fresh_context.NL_OUTPUT_SCHEMA)
        self.assertEqual(host.call_args.kwargs["timeout_seconds"], 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
