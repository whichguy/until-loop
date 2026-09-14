"""Runner boundaries without live model calls."""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality as quality
from improve_quality_fixtures import apply_reference, prepare_case


class QualityRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-quality-runner-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source(self, linked=False):
        source = self.root / "source"
        source.mkdir()
        quality.command(source, "git", "init", "-q")
        quality.git(source, "config", "user.name", "Fixture")
        quality.git(source, "config", "user.email", "fixture@example.invalid")
        for name in ("SKILL.md", "references/review-policy.md", "runtime/until-loop/ADAPTER.md"):
            path = source / "skills/improve" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen original " + name)
        if linked:
            (source / "skills/improve/link").symlink_to("SKILL.md")
        quality.git(source, "add", "skills")
        quality.git(source, "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Seed")
        return source

    def fixture(self):
        workspace, evidence = self.root / "candidate", self.root / "evidence"
        manifest = prepare_case("blank_fallback", workspace, evidence)
        return workspace, evidence, manifest

    def test_freeze_reads_commit_not_dirty_worktree(self):
        source = self.source()
        (source / "skills/improve/SKILL.md").write_text("uncommitted replacement")
        result = quality.freeze(source, "HEAD", self.root / "frozen")
        self.assertEqual((self.root / "frozen/SKILL.md").read_text(), "frozen original SKILL.md")
        self.assertEqual(result["digest"], quality.digest(quality.canonical(quality.tree(self.root / "frozen"))))
        with self.assertRaises(ValueError):
            quality.freeze(source, "HEAD", self.root / "frozen")

    def test_freeze_rejects_symlink_dependency(self):
        source = self.source(linked=True)
        with self.assertRaisesRegex(ValueError, "regular files"):
            quality.freeze(source, "HEAD", self.root / "frozen")

    def test_manifest_does_not_follow_external_link(self):
        outside = self.root / "private.txt"
        outside.write_text("held-out secret")
        candidate = self.root / "small"
        candidate.mkdir()
        (candidate / "link").symlink_to(outside)
        result = quality.tree(candidate)
        self.assertEqual(result["link"]["kind"], "symlink")
        self.assertNotIn("held-out secret", json.dumps(result))

    def test_manifest_bounds_evidence_size(self):
        (self.root / "large").write_bytes(b"abcd")
        with mock.patch.object(quality, "MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "bounded snapshot"):
                quality.tree(self.root)

    def test_capture_rejects_symlink_candidate(self):
        workspace, evidence, _ = self.fixture()
        (workspace / "shortcut").symlink_to(workspace / "formatter.py")
        with self.assertRaisesRegex(ValueError, "cannot be faithfully audited"):
            quality.capture(workspace, evidence, 0, "linked candidate")

    def test_capture_retains_actual_candidate_and_git_identity(self):
        workspace, evidence, manifest = self.fixture()
        captured = quality.capture(workspace, evidence, 0, "baseline")
        self.assertTrue(captured["stable"])
        self.assertEqual(captured["head"], manifest["baseline_head"])
        self.assertEqual((evidence / "snapshots/s0000/candidate/formatter.py").read_bytes(), (workspace / "formatter.py").read_bytes())
        self.assertFalse((evidence / "snapshots/s0000/candidate/.git").exists())
        self.assertIn("snapshots/s0000/git.json", captured["relative_evidence_paths"])

    def test_capture_reports_inventory_drift_without_assigning_a_cause(self):
        workspace, evidence, _ = self.fixture()
        before = quality.tree(workspace)
        after = {path: dict(item) for path, item in before.items()}
        after["formatter.py"]["sha256"] = "0" * 64
        with mock.patch.object(quality, "tree", side_effect=[before, after]):
            captured = quality.capture(workspace, evidence, 0, "synthetic inventory drift")
        stability = captured["stability"]
        self.assertFalse(captured["stable"])
        self.assertEqual(stability["inventory_changed_paths"], ["formatter.py"])
        self.assertEqual(stability["inventory_before_sha256"], quality.digest(quality.canonical(before)))
        self.assertEqual(stability["inventory_after_sha256"], quality.digest(quality.canonical(after)))
        self.assertTrue(stability["head"]["stable"])
        self.assertTrue(stability["index"]["stable"])
        self.assertEqual(
            json.loads((evidence / "snapshots/s0000/git.json").read_text())["stability"], stability
        )
        self.assertEqual(
            json.loads((evidence / "snapshots/s0000/snapshot.json").read_text())["stability"], stability
        )

    def test_capture_reports_head_and_index_drift(self):
        workspace, evidence, _ = self.fixture()
        original_git = quality.git
        initial_head = original_git(workspace, "rev-parse", "HEAD").strip()
        initial_index = original_git(workspace, "ls-files", "--stage")
        heads = iter((initial_head, "different-head"))
        indexes = iter((initial_index, "different-index\n"))

        def drifting_git(root, *args):
            if args == ("rev-parse", "HEAD"):
                return next(heads)
            if args == ("ls-files", "--stage"):
                return next(indexes)
            return original_git(root, *args)

        with mock.patch.object(quality, "git", side_effect=drifting_git):
            captured = quality.capture(workspace, evidence, 0, "synthetic Git drift")
        stability = captured["stability"]
        self.assertFalse(captured["stable"])
        self.assertEqual(stability["inventory_changed_paths"], [])
        self.assertEqual(stability["head"], {
            "before": initial_head, "after": "different-head", "stable": False,
        })
        self.assertEqual(stability["index"], {
            "before_sha256": quality.digest(initial_index.encode()),
            "after_sha256": quality.digest(b"different-index\n"),
            "stable": False,
        })

    def test_capture_attempts_are_append_only_and_keep_partial_bytes(self):
        evidence = self.root / "evidence"

        def partial(label):
            path = evidence / "snapshots/s0001/candidate" / (label + ".txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(label)

        partial("first")
        first = quality.capture_attempt(evidence, 1, "observed runtime state change", ValueError("first failure"))
        partial("second")
        second = quality.capture_attempt(evidence, 1, "after invocation", ValueError("second failure"))

        self.assertEqual([first["id"], second["id"]], ["a0000", "a0001"])
        self.assertEqual(
            (evidence / first["partial_snapshot_ref"] / "candidate/first.txt").read_text(), "first"
        )
        self.assertEqual(
            (evidence / second["partial_snapshot_ref"] / "candidate/second.txt").read_text(), "second"
        )
        self.assertEqual(json.loads((evidence / first["attempt_ref"]).read_text()), first)
        self.assertEqual(json.loads((evidence / second["attempt_ref"]).read_text()), second)

    def test_run_retains_mid_copy_failure_outside_successful_snapshots(self):
        source = self.source()
        root = self.root / "trial"
        quality.prepare(source, "HEAD", root, "blank_fallback")
        workspace = root / "candidate"
        evidence = root / "evidence"
        (workspace / ".until-loop").mkdir()
        (workspace / ".until-loop/state.json").write_text(json.dumps({
            "phase": "active", "cycle": 1, "action": {"id": "synthetic-capture"},
        }))
        original_capture = quality.capture
        failed_once = False

        def mid_copy_failure(candidate, retained_evidence, number, reason):
            nonlocal failed_once
            if reason == "observed runtime state change" and not failed_once:
                failed_once = True
                partial = retained_evidence / "snapshots" / ("s%04d" % number) / "candidate"
                partial.mkdir(parents=True)
                (partial / "partial.txt").write_text("truncated capture")
                raise ValueError("synthetic mid-copy failure")
            return original_capture(candidate, retained_evidence, number, reason)

        process = mock.Mock(returncode=0)
        process.stdin = mock.Mock()
        process.poll.side_effect = [None, 0, 0]
        original_command = quality.command
        original_popen = subprocess.Popen

        def local_command(candidate, *args):
            if args == ("codex", "--version"):
                return "synthetic transport"
            return original_command(candidate, *args)

        def launch(argv, *args, **kwargs):
            return process if argv[0] == "codex" else original_popen(argv, *args, **kwargs)

        with mock.patch.object(quality, "capture", side_effect=mid_copy_failure), \
                mock.patch.object(quality, "command", side_effect=local_command), \
                mock.patch.object(quality.time, "sleep"), \
                mock.patch.object(quality.subprocess, "Popen", side_effect=launch):
            observed = quality.run(root, timeout=10)

        self.assertEqual(observed["trial_status"], "incomplete")
        self.assertIn("synthetic mid-copy failure", observed["capture_errors"])
        self.assertEqual([snapshot["id"] for snapshot in observed["snapshots"]], ["s0000", "s0001"])
        self.assertEqual(observed["snapshots"][-1]["reason"], "after invocation")
        self.assertEqual(len(observed["capture_attempts"]), 1)
        attempt = observed["capture_attempts"][0]
        self.assertEqual(attempt["id"], "a0000")
        self.assertEqual(attempt["intended_snapshot_id"], "s0001")
        self.assertEqual(attempt["partial_snapshot_ref"], "capture-attempts/a0000/partial-snapshot")
        self.assertIn(attempt["attempt_ref"], observed["evidence_refs"])
        self.assertIn(attempt["partial_snapshot_ref"], observed["evidence_refs"])
        self.assertEqual(
            json.loads((evidence / attempt["attempt_ref"]).read_text()), attempt
        )
        self.assertEqual(
            (evidence / attempt["partial_snapshot_ref"] / "candidate/partial.txt").read_text(), "truncated capture"
        )
        self.assertFalse((evidence / "snapshots/s0001/candidate/partial.txt").exists())
        integrity = observed["source_integrity_observations"]
        self.assertTrue(integrity["frozen_source_digest_matches"])
        self.assertFalse(integrity["read_receipts_complete"])
        self.assertFalse(integrity["forbidden_access_observed"])

    def test_unchanged_reviews_may_share_candidate_digest(self):
        workspace, evidence, _ = self.fixture()
        first = quality.capture(workspace, evidence, 0, "review one")
        (workspace / ".until-loop").mkdir()
        (workspace / ".until-loop/working.md").write_text("Another review record.")
        second = quality.capture(workspace, evidence, 1, "review two")
        self.assertEqual(first["candidate_digest"], second["candidate_digest"])
        self.assertNotEqual(first["manifest"], second["manifest"])

    def test_preservation_accepts_scoped_commit_but_rejects_user_edit(self):
        workspace, _, manifest = self.fixture()
        apply_reference("blank_fallback", workspace)
        self.assertTrue(quality.preserved(workspace, manifest["protected"]))
        (workspace / "notes/in-progress.txt").write_text("overwritten")
        self.assertFalse(quality.preserved(workspace, manifest["protected"]))

    def test_preservation_rejects_absorbing_staged_user_work(self):
        workspace, _, manifest = self.fixture()
        quality.git(workspace, "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Absorb draft")
        # Same staged blob is not sufficient: the staged draft must still be
        # pending rather than silently included in a new product commit.
        self.assertFalse(quality.preserved(workspace, manifest["protected"]))

    def test_events_skip_partial_json_and_reasoning(self):
        path = self.root / "events.jsonl"
        path.write_text(json.dumps({"item": {"type": "reasoning", "text": "private"}}) + "\n" +
                        json.dumps({"type": "turn.completed"}) + "\n{partial")
        self.assertEqual(quality.read_events(path), [{"type": "turn.completed"}])
        final = quality.read_events(path, complete=True)
        self.assertEqual(final[-1]["type"], "evaluator.parse_error")

    def test_malformed_completed_event_is_not_silently_dropped(self):
        path = self.root / "events.jsonl"
        path.write_text('{broken command}\n[]\n')
        self.assertEqual([event["type"] for event in quality.read_events(path)], ["evaluator.parse_error"] * 2)

    def test_relative_evaluator_access_is_flagged(self):
        events = [{"type": "item.started", "item": {"command": command}}
                  for command in ("cat ../evidence/manifest.json", "find .. -type f", "cat ../package/SKILL.md")]
        self.assertEqual(quality.forbidden_accesses(events, self.root / "evidence"), [0, 1])

    def test_limits_reject_bool_as_number(self):
        with self.assertRaisesRegex(ValueError, "invalid execution limits"):
            quality.run(self.root, timeout=True)

    def test_terminal_and_paused_states_have_no_current_action(self):
        for phase in ("done", "paused", "halted"):
            with self.subTest(phase=phase):
                self.assertEqual(quality.observation_key({"cycle": 3, "phase": phase, "action": None}),
                                 (3, phase, None, None))

    def test_fake_host_terminal_transition_is_observed_without_a_model(self):
        source = self.source()
        root = self.root / "trial"
        manifest = quality.prepare(source, "HEAD", root, "blank_fallback")
        driver = self.root / "synthetic_transport.py"
        driver.write_text("""import json,sys,time
from pathlib import Path
sys.stdin.read()
run=Path('.until-loop');run.mkdir()
state={'phase':'active','cycle':0,'action':{'id':'synthetic'}}
(run/'state.json').write_text(json.dumps(state))
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(0.5)
state.update(phase='done',cycle=1,action=None)
(run/'state.json').write_text(json.dumps(state))
time.sleep(0.5)
print(json.dumps({'type':'turn.completed'}),flush=True)
""")
        original_command = quality.command
        def local_command(workspace, *args):
            if args == ("codex", "--version"):
                return "synthetic transport, no model"
            return original_command(workspace, *args)
        with mock.patch.object(quality, "execution_argv", return_value=[sys.executable, str(driver)]), mock.patch.object(quality, "command", side_effect=local_command):
            observed = quality.run(root, timeout=10)
        self.assertEqual(observed["runtime_phase"], "done")
        self.assertEqual(observed["trial_status"], "completed")
        self.assertFalse(observed["behavior_passed"])
        self.assertFalse(observed["source_integrity"])
        self.assertEqual(observed["snapshots"][-1]["state"]["action"], None)

    def test_execution_uses_one_supported_permission_mode(self):
        argv = quality.execution_argv(self.root / "candidate", self.root / "evidence")
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_requalification_distinguishes_surviving_mutant_from_inconclusive_probe(self):
        manifest = {"workspace": str(self.root), "case_id": "clean_control", "fixture": {}}
        probe = {"passed": False, "baseline": {"failures": [], "errors": []},
                 "mutation_outcome": "inconclusive"}
        with mock.patch.object(quality, "run_test_quality", return_value=probe):
            self.assertEqual(quality.requalification(manifest, [])["status"], "incomplete")
            probe["mutation_outcome"] = "survived"
            self.assertEqual(quality.requalification(manifest, [])["status"], "fail")
            probe["mutation_outcome"] = "detected"
            probe["passed"] = True
            self.assertEqual(quality.requalification(manifest, [])["status"], "pass")

    def test_retry_allows_transport_error_not_invalid_semantic_answer(self):
        directory = self.root / "audit"
        directory.mkdir()
        events = directory / "events.jsonl"
        events.write_text(json.dumps({"type": "turn.failed", "error": {"message": "invalid_json_schema"}}) + "\n")
        self.assertTrue(quality.transport_retry_allowed(directory))
        (directory / "judgment.json").write_text('{bad answer')
        self.assertFalse(quality.transport_retry_allowed(directory))
        (directory / "judgment.json").unlink()
        events.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "I judge this incomplete"}}) + "\n" + events.read_text())
        self.assertFalse(quality.transport_retry_allowed(directory))

    def test_read_markers_require_successful_observed_output(self):
        source = self.root / "package"
        markers = "Standalone owner binding Policy ID: improve/review-policy/v1 Interpret the contract until-loop"
        paths = [source / p for p in ("SKILL.md", "references/review-policy.md", "runtime/until-loop/ADAPTER.md")]
        event = {"type": "item.completed", "item": {"command": "cat " + " ".join(map(str, paths)), "exit_code": 1, "aggregated_output": markers}}
        self.assertFalse(all(quality.source_read_evidence([event], source).values()))
        event["item"]["exit_code"] = 0
        self.assertTrue(all(quality.source_read_evidence([event], source).values()))

    def test_printed_markers_do_not_establish_package_reads(self):
        source = self.root / "package"
        printed = str(source / "SKILL.md") + ' Standalone owner binding Policy ID: improve/review-policy/v1 Interpret the contract'
        event = {"type": "item.completed", "item": {"command": "printf '" + printed + "'", "exit_code": 0, "aggregated_output": printed}}
        self.assertFalse(any(quality.source_read_evidence([event], source).values()))
        event["item"]["command"] = "cat " + str(source / "SKILL.md.decoy")
        self.assertFalse(quality.source_read_evidence([event], source)["card"])

    def test_constructed_python_read_is_bound_to_selected_root_and_file(self):
        source = self.root / "package"
        command = "python3 - <<'PY'\nfrom pathlib import Path\np=Path(" + repr(str(source / "SKILL.md")) + ").resolve().parent\nfor rel in ['references/review-policy.md','runtime/until-loop/ADAPTER.md','references/evidence-capture.md']:\n print('\\nFILE', p/rel)\n print((p/rel).read_text())\nPY"
        event = {"type": "item.completed", "item": {"command": command, "exit_code": 0,
                 "aggregated_output": str(source / "references/review-policy.md") + "\nPolicy ID: improve/review-policy/v1\n" + str(source / "runtime/until-loop/ADAPTER.md") + "\nInterpret the contract"}}
        result = quality.source_read_evidence([event], source)
        self.assertTrue(result["policy"])
        self.assertTrue(result["adapter"])
        self.assertFalse(result["card"])

    def test_python_comments_and_unreachable_reads_do_not_establish_source_reads(self):
        source = self.root / "package"
        prefix = "from pathlib import Path\np=Path(" + repr(str(source / "SKILL.md")) + ").resolve().parent\n"
        scripts = {
            "commented": prefix + "for rel in ['references/review-policy.md']:\n print('\\nFILE', p/rel)\n print('Policy ID: improve/review-policy/v1')\n # print((p/rel).read_text())\n",
            "conditional": prefix + "for rel in ['references/review-policy.md']:\n print('\\nFILE', p/rel)\n print('Policy ID: improve/review-policy/v1')\nif False:\n print((p/'references/review-policy.md').read_text())\n",
        }
        for label, script in scripts.items():
            with self.subTest(label=label):
                command = "python3 - <<'PY'\n" + script + "PY"
                event = {"type": "item.completed", "item": {"command": command, "exit_code": 0,
                         "aggregated_output": "Policy ID: improve/review-policy/v1"}}
                self.assertFalse(quality.source_read_evidence([event], source)["policy"])

    def test_request_does_not_reveal_fixture_answers_or_pass_quota(self):
        manifest = {"source": {"path": "/frozen"}, "fixture": {"request": "Improve the candidate."}}
        prompt = quality.execution_prompt(manifest)
        self.assertIn("Improve the candidate.", prompt)
        self.assertIn("No network", prompt)
        for secret in ("blank_fallback", "Anonymous", "three reviews", "four reviews", "apply_reference"):
            self.assertNotIn(secret, prompt)

    def test_run_cannot_reuse_prior_invocation(self):
        source = self.source()
        root = self.root / "trial"
        quality.prepare(source, "HEAD", root, "blank_fallback")
        (root / "evidence/invocation.json").write_text('{}')
        with mock.patch.object(quality.subprocess, "Popen") as host:
            # Source hashing precedes the exclusive marker, but no host may run.
            with self.assertRaises(FileExistsError):
                quality.run(root)
            host.assert_not_called()

    def test_audit_cannot_erase_prior_source_integrity_failure(self):
        source = self.source()
        root = self.root / "trial"
        manifest = quality.prepare(source, "HEAD", root, "blank_fallback")
        evidence = root / "evidence"
        snapshots = json.loads((evidence / "snapshots.json").read_text())
        quality.write_json(evidence / "observed.json", {
            "source_integrity": False, "trial_status": "completed",
            "snapshots": snapshots, "evidence_refs": [],
        })
        (evidence / "events.jsonl").write_text("")
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (None, None)
        original_popen = subprocess.Popen
        def launch(argv, **kwargs):
            return process if argv[0] == "codex" else original_popen(argv, **kwargs)
        with mock.patch.object(quality, "source_read_evidence", return_value={
                    "card": True, "policy": True, "adapter": True, "selected_path_observed": True,
                }), \
                mock.patch.object(quality, "requalification", return_value={"passed": True}), \
                mock.patch.object(quality, "build_judge_prompt", return_value="audit"), \
                mock.patch.object(quality.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(quality, "validate_and_grade", return_value={"status": "incomplete"}) as grade:
            quality.audit(root)
        self.assertFalse(grade.call_args.args[1]["source_integrity"])
        integrity = grade.call_args.args[1]["audit_source_integrity_observations"]
        self.assertTrue(integrity["frozen_source_digest_matches"])
        self.assertTrue(integrity["read_receipts_complete"])
        self.assertFalse(integrity["forbidden_access_observed"])


if __name__ == "__main__":
    unittest.main()
