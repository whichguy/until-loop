#!/usr/bin/env python3
"""CLI regressions for decisions an agent must make from a v2 packet.

These tests deliberately compose the established protocol fixture helpers rather
than inherit their test case, so discovery does not run the v2 suite twice.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import unittest
from pathlib import Path

import test_v2 as _test_v2


class PacketDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = _test_v2.UntilLoopV2ProtocolTests()
        self.protocol.setUp()

    def tearDown(self) -> None:
        try:
            self.protocol.doCleanups()
        finally:
            self.protocol.tearDown()

    def write_provenance(self, repo: Path, name: str, kind: str) -> Path:
        path = repo / name
        self.protocol.write_json(
            path,
            {"provenance": {"kind": kind, "reference": f"recorded {kind}"}},
        )
        return path

    def submit_blocked(self, repo: Path, *, resume_on: str) -> str:
        assessment = self.protocol.assessment(
            repo,
            decision="blocked",
            next_action=None,
            blocker={
                "reason": "An external condition is required.",
                "resumption_condition": "The required condition is recorded.",
                "resume_on": resume_on,
            },
        )
        action_id = self.protocol.write_assessment(repo, assessment)
        self.protocol.assert_returncode(self.protocol.submit(repo, action_id), 0)
        return action_id

    def assert_fresh_submit_rejected(self, repo: Path, accepted_id: str) -> None:
        fresh_id = "f" * 32 if accepted_id != "f" * 32 else "e" * 32
        result = self.protocol.submit(repo, fresh_id)
        self.protocol.assert_returncode(result, 2)

    def nonwork_record_and_command(self, packet: str, subcommand: str) -> tuple[dict[str, object], list[str]]:
        records = re.findall(r"```json\n(.*?)\n```", packet, flags=re.DOTALL)
        self.assertEqual(len(records), 1)
        record = json.loads(records[0])
        commands = [
            line.strip() for line in packet.splitlines()
            if f" {subcommand} --repo " in line
        ]
        self.assertEqual(len(commands), 1)
        return record, shlex.split(commands[0])

    def test_condition_observed_pause_rejects_user_instruction_and_resumes(self) -> None:
        repo = self.protocol.repository("condition-observed")
        self.protocol.initialize(repo, verify=self.protocol.counting_verifier())
        old_action = self.submit_blocked(repo, resume_on="condition_observed")
        paused = self.protocol.state(repo)
        self.assertEqual(paused["phase"], "paused")
        self.assertEqual(paused["cycle"], 0)

        wrong = self.write_provenance(repo, "wrong.json", "user_instruction")
        rejected = self.protocol.run_cli(
            "v2", "resume", "--repo", str(repo), "--provenance-file", str(wrong)
        )
        self.protocol.assert_returncode(rejected, 2)
        self.assertEqual(self.protocol.progress_key(self.protocol.state(repo)),
                         self.protocol.progress_key(paused))

        observed = self.write_provenance(repo, "observed.json", "condition_observed")
        resumed = self.protocol.run_cli(
            "v2", "resume", "--repo", str(repo), "--provenance-file", str(observed)
        )
        self.protocol.assert_returncode(resumed, 0)
        active = self.protocol.state(repo)
        self.assertEqual(active["phase"], "active")
        self.assertEqual(active["cycle"], 0)
        self.assertNotEqual(active["action"]["id"], old_action)
        self.assertEqual(self.protocol.verifier_count(repo), 0)

    def test_nonactive_states_reject_fresh_submit_and_illegal_resume_but_replay_receipt(self) -> None:
        cases = (("done", None), ("halted", 1), ("paused", None))
        for phase, max_cycles in cases:
            with self.subTest(phase=phase):
                repo = self.protocol.repository(f"nonactive-{phase}")
                self.protocol.initialize(repo, max_cycles=max_cycles)
                if phase == "paused":
                    accepted_id = self.submit_blocked(repo, resume_on="user_instruction")
                    resume_file = self.write_provenance(repo, "illegal.json", "condition_observed")
                else:
                    decision = "complete" if phase == "done" else "continue"
                    assessment = self.protocol.assessment(
                        repo,
                        decision=decision,
                        statuses={"C1": "satisfied", "C2": "satisfied"} if decision == "complete" else None,
                        next_action="Continue until the cycle limit." if decision == "continue" else None,
                    )
                    accepted_id = self.protocol.write_assessment(repo, assessment)
                    self.protocol.assert_returncode(self.protocol.submit(repo, accepted_id), 0)
                    resume_file = self.write_provenance(repo, "illegal.json", "user_instruction")

                state = self.protocol.state(repo)
                self.assertEqual(state["phase"], phase)
                self.assert_fresh_submit_rejected(repo, accepted_id)
                illegal_resume = self.protocol.run_cli(
                    "v2", "resume", "--repo", str(repo), "--provenance-file", str(resume_file)
                )
                self.protocol.assert_returncode(illegal_resume, 2)
                replay = self.protocol.submit(repo, accepted_id)
                self.protocol.assert_returncode(replay, 0)
                self.assertEqual(self.protocol.progress_key(self.protocol.state(repo)),
                                 self.protocol.progress_key(state))

    def test_failed_verifier_issues_new_action_while_rejection_retains_action(self) -> None:
        failed_repo = self.protocol.repository("failed-verifier-new-action")
        failing = self.protocol.python_verify("raise SystemExit(1)")
        self.protocol.initialize(failed_repo, verify=failing)
        old_failed, _ = self.protocol.action(failed_repo)
        complete = self.protocol.assessment(
            failed_repo,
            decision="complete",
            statuses={"C1": "satisfied", "C2": "satisfied"},
        )
        self.protocol.write_assessment(failed_repo, complete)
        self.protocol.assert_returncode(self.protocol.submit(failed_repo, old_failed), 0)
        failed_state = self.protocol.state(failed_repo)
        self.assertEqual(failed_state["phase"], "active")
        self.assertEqual(failed_state["last_verify"]["ok"], False)
        self.assertNotEqual(failed_state["action"]["id"], old_failed)

        rejected_repo = self.protocol.repository("malformed-rejection-same-action")
        self.protocol.initialize(rejected_repo)
        old_rejected, _ = self.protocol.action(rejected_repo)
        malformed = self.protocol.assessment(
            rejected_repo, decision="complete", statuses={"C1": "satisfied"}
        )
        self.protocol.write_assessment(rejected_repo, malformed)
        self.protocol.assert_returncode(self.protocol.submit(rejected_repo, old_rejected), 2)
        rejected_state = self.protocol.state(rejected_repo)
        self.assertEqual(rejected_state["phase"], "active")
        self.assertIsNone(rejected_state["last_verify"])
        self.assertEqual(rejected_state["action"]["id"], old_rejected)

    def test_active_callback_parses_and_executes_for_workspace_with_whitespace(self) -> None:
        repo = self.protocol.repository("workspace with whitespace")
        packet = self.protocol.initialize(repo).stdout.decode("utf-8", errors="replace")
        callbacks = [line.strip() for line in packet.splitlines()
                     if "until_loop_v2.py submit --repo" in line]
        self.assertEqual(len(callbacks), 1)
        callback = shlex.split(callbacks[0])
        repo_argument = callback[callback.index("--repo") + 1]
        self.assertEqual(Path(repo_argument).resolve(), repo.resolve())
        action_id = self.protocol.write_assessment(
            repo,
            self.protocol.assessment(
                repo, decision="continue", next_action="Inspect the next criterion."
            ),
        )
        self.assertIn(action_id, callback)
        executed = self.protocol.run_argv(callback)
        self.protocol.assert_returncode(executed, 0)
        state = self.protocol.state(repo)
        self.assertEqual(state["cycle"], 1)
        self.assertNotEqual(state["action"]["id"], action_id)

    def test_paused_packet_template_resumes_after_filling_only_reference_and_path(self) -> None:
        repo = self.protocol.repository("packet-pause-template")
        self.protocol.initialize(repo)
        old_action = self.submit_blocked(repo, resume_on="condition_observed")
        packet = self.protocol.run_cli("v2", "next", "--repo", str(repo)).stdout.decode("utf-8")
        record, command = self.nonwork_record_and_command(packet, "resume")
        self.assertEqual(record["provenance"]["kind"], "condition_observed")
        record["provenance"]["reference"] = "fixture observed required condition"
        provenance = repo / "packet-provenance.json"
        self.protocol.write_json(provenance, record)
        command[command.index("<absolute-provenance-json>")] = str(provenance)

        resumed = self.protocol.run_argv(command)
        self.protocol.assert_returncode(resumed, 0)
        state = self.protocol.state(repo)
        self.assertEqual(state["phase"], "active")
        self.assertEqual(state["cycle"], 0)
        self.assertNotEqual(state["action"]["id"], old_action)

    def test_uncertainty_packet_template_resolves_after_filling_only_reference_and_path(self) -> None:
        repo = self.protocol.repository("packet-recovery-template")
        self.protocol.initialize(repo, verify=self.protocol.counting_verifier())
        old_action, _ = self.protocol.action(repo)
        self.protocol.write_assessment(
            repo,
            self.protocol.assessment(repo, decision="continue", next_action="Inspect uncertainty."),
        )
        crashed = self.protocol.run_cli(
            "v2", "submit", "--repo", str(repo), "--action-id", old_action,
            env={"UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER": "1"},
        )
        self.protocol.assert_returncode(crashed, 70)
        packet = self.protocol.run_cli("v2", "next", "--repo", str(repo)).stdout.decode("utf-8")
        record, command = self.nonwork_record_and_command(packet, "resolve-verifier")
        self.assertEqual(record["action_id"], old_action)
        self.assertEqual(record["resolution"], "abandon")
        record["provenance"]["reference"] = "fixture inspected verifier effects"
        resolution = repo / "packet-resolution.json"
        self.protocol.write_json(resolution, record)
        command[command.index("<absolute-resolution-json>")] = str(resolution)

        resolved = self.protocol.run_argv(command)
        self.protocol.assert_returncode(resolved, 0)
        state = self.protocol.state(repo)
        self.assertEqual(state["phase"], "active")
        self.assertEqual(state["cycle"], 0)
        self.assertIsNone(state["recovery"])
        self.assertNotEqual(state["action"]["id"], old_action)
        self.assertEqual(self.protocol.verifier_count(repo), 1)

    def test_current_next_reads_a_legacy_frozen_policy_snapshot(self) -> None:
        repo = self.protocol.repository("legacy-policy-snapshot")
        runtime_root = self.protocol.root / "legacy-runtime"
        (runtime_root / "scripts").mkdir(parents=True)
        (runtime_root / "references").mkdir()
        for name in ("until-loop", "until_loop_v2.py", "until_loop_packet.py"):
            shutil.copy2(Path(__file__).parents[1] / "scripts" / name, runtime_root / "scripts" / name)
        maintained_policy = Path(__file__).parents[1] / "references" / "decision-rubric.md"
        policy_text = maintained_policy.read_text(encoding="utf-8")
        current_policy = json.loads(re.search(r"```json\n(.*?)\n```", policy_text, re.DOTALL).group(1))
        old_policy = json.loads(json.dumps(current_policy))
        old_policy["version"] = "decision-rubric/1"
        old_instruction = "Legacy frozen instruction: preserve this exact wording."
        old_policy["questions"][0]["instruction"] = old_instruction
        legacy_policy_path = runtime_root / "references" / "decision-rubric.md"
        legacy_policy_path.write_text(
            "# Legacy policy fixture\n\n```json\n" + json.dumps(old_policy, indent=2) + "\n```\n",
            encoding="utf-8",
        )
        contract = self.protocol.contract(policy="decision-rubric/1")
        contract_path = repo / "contract.json"
        self.protocol.write_json(contract_path, contract)
        legacy_cli = runtime_root / "scripts" / "until_loop_v2.py"
        initialized = self.protocol.run_argv([
            sys.executable, str(legacy_cli), "init", "--repo", str(repo),
            "--contract-file", str(contract_path),
        ])
        self.protocol.assert_returncode(initialized, 0)
        state_before = self.protocol.state_path(repo).read_bytes()

        legacy_policy_path.write_text(policy_text, encoding="utf-8")
        packet = self.protocol.run_argv([sys.executable, str(legacy_cli), "next", "--repo", str(repo)])
        self.protocol.assert_returncode(packet, 0)
        rendered = packet.stdout.decode("utf-8", errors="replace")
        self.assertIn("decision-rubric/1", rendered)
        self.assertIn(old_instruction, rendered)
        self.assertNotIn(current_policy["questions"][0]["instruction"], rendered)
        self.assertEqual(self.protocol.state_path(repo).read_bytes(), state_before)


if __name__ == "__main__":
    unittest.main()
