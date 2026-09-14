#!/usr/bin/env python3
"""Seeded interaction experiments for the public until-loop v2 CLI.

The protocol tests cover individual boundaries.  This suite composes those
boundaries through real subprocesses and retains an optional event ledger for
an external validation run.  Its assertions inspect durable state and receipts
instead of replaying the runtime's transition logic in a second model.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLI = SKILL_ROOT / "scripts" / "until-loop"
RUN_DIRNAME = ".until-loop"
EVIDENCE_ENV = "UNTIL_LOOP_V2_EXPERIMENT_EVIDENCE"
SEEDS = (1729, 271828, 314159, 8675309)


class GeneratedRuntimeSequenceTests(unittest.TestCase):
    """Exercise interacting runtime controls without touching production code."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="until-loop-v2-generated-")
        self.root = Path(self._temporary.name)
        self.events: List[Dict[str, Any]] = []
        self.case_summaries: List[Dict[str, Any]] = []

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_cli(
        self,
        *arguments: str,
        env: Optional[Dict[str, Optional[str]]] = None,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess:
        command_env = self.command_env(env)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=str(self.root),
            env=command_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    @staticmethod
    def command_env(extra: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, str]:
        environment = {"PATH": os.defpath, "LC_ALL": "C", "LANG": "C"}
        if extra:
            for key, value in extra.items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
        return environment

    def repository(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True)
        return repo

    @staticmethod
    def run_dir(repo: Path) -> Path:
        return repo / RUN_DIRNAME

    def state_path(self, repo: Path) -> Path:
        return self.run_dir(repo) / "state.json"

    def state(self, repo: Path) -> Dict[str, Any]:
        return json.loads(self.state_path(repo).read_text(encoding="utf-8"))

    def history(self, repo: Path) -> List[Dict[str, Any]]:
        path = self.run_dir(repo) / "history.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def history_bytes(self, repo: Path) -> bytes:
        path = self.run_dir(repo) / "history.jsonl"
        return path.read_bytes() if path.exists() else b""

    def action(self, repo: Path) -> Tuple[str, Path]:
        action = self.state(repo)["action"]
        self.assertIsInstance(action, dict)
        action_id = action["id"]
        self.assertIsInstance(action_id, str)
        self.assertRegex(action_id, r"\A[a-f0-9]{32}\Z")
        result_path = Path(action["result_path"])
        self.assertEqual(
            result_path.resolve(),
            (self.run_dir(repo) / "results" / (action_id + ".json")).resolve(),
        )
        return action_id, result_path

    @staticmethod
    def criterion(identifier: str) -> Dict[str, Any]:
        return {
            "id": identifier,
            "text": "Requirement {}".format(identifier),
            "basis": {
                "kind": "request",
                "reference": "original request clause {}".format(identifier),
            },
        }

    def contract(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "policy": "decision-rubric/2",
            "original_request": "Make the importer robust and document its use.",
            "interpretation": "Preserve every criterion and reassess current evidence.",
            "criteria": [self.criterion("C1"), self.criterion("C2")],
        }

    @staticmethod
    def write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def state_digest(state: Dict[str, Any]) -> str:
        payload = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def python_verify(code: str) -> str:
        return "{} -c {}".format(shlex.quote(sys.executable), shlex.quote(code))

    def counting_verifier(self) -> str:
        return self.python_verify(
            "from pathlib import Path\n"
            "p = Path('verifier-count')\n"
            "p.write_text(str(int(p.read_text() if p.exists() else '0') + 1) + '\\n')\n"
        )

    @staticmethod
    def verifier_count(repo: Path) -> int:
        path = repo / "verifier-count"
        return int(path.read_text(encoding="utf-8")) if path.exists() else 0

    def observed(self, repo: Path) -> Optional[Dict[str, Any]]:
        if not self.state_path(repo).exists():
            return None
        state = self.state(repo)
        action = state.get("action")
        recovery = state.get("recovery")
        return {
            "phase": state.get("phase"),
            "cycle": state.get("cycle"),
            "contract_revision": state.get("contract", {}).get("revision"),
            "action_id": action.get("id") if isinstance(action, dict) else None,
            "recovery_action_id": recovery.get("action_id")
            if isinstance(recovery, dict)
            else None,
            "state_digest": self.state_digest(state),
            "history_events": len(self.history(repo)),
            "verifier_count": self.verifier_count(repo),
        }

    def assert_observed_state_invariants(self, repo: Path) -> None:
        """Check externally visible relations, not a shadow transition table."""
        state = self.state(repo)
        self.assertEqual(state["version"], 2)
        phase = state["phase"]
        self.assertIn(phase, ("active", "paused", "done", "halted"))
        self.assertIsInstance(state["cycle"], int)
        self.assertGreaterEqual(state["cycle"], 0)
        self.assertLessEqual(state["cycle"], state["max_cycles"])
        contract = state["contract"]
        self.assertIsInstance(contract["revision"], int)
        self.assertGreaterEqual(contract["revision"], 1)

        action = state["action"]
        if phase == "active":
            self.assertIsInstance(action, dict)
        else:
            self.assertIsNone(action)
        if isinstance(action, dict):
            self.assertRegex(action["id"], r"\A[a-f0-9]{32}\Z")
            self.assertEqual(action["contract_revision"], contract["revision"])
            self.assertEqual(
                Path(action["result_path"]).resolve(),
                (self.run_dir(repo) / "results" / (action["id"] + ".json")).resolve(),
            )

        recovery = state["recovery"]
        if isinstance(recovery, dict):
            self.assertEqual(phase, "active")
            self.assertIsInstance(action, dict)
            self.assertEqual(recovery["action_id"], action["id"])
            self.assertEqual(recovery["cycle"], state["cycle"] + 1)

        if phase == "paused":
            self.assertIsInstance(state["pause"], dict)
            self.assertEqual(state["last_assessment"]["decision"], "blocked")
        else:
            self.assertIsNone(state["pause"])

        history = self.history(repo)
        if history and recovery is None:
            self.assertEqual(history[-1]["state_digest"], self.state_digest(state))

    def durable_snapshot(self, repo: Path) -> Dict[str, Any]:
        return {
            "state": self.state_path(repo).read_bytes(),
            "history": self.history_bytes(repo),
            "verifier_count": self.verifier_count(repo),
        }

    def assert_durable_snapshot_equal(
        self, before: Dict[str, Any], repo: Path, label: str
    ) -> None:
        self.assertEqual(self.durable_snapshot(repo), before, label)

    @staticmethod
    def output_tail(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")[-800:]

    def assert_returncode(
        self, result: subprocess.CompletedProcess, expected: int, label: str
    ) -> None:
        self.assertEqual(
            result.returncode,
            expected,
            "{}: stdout tail={!r}; stderr tail={!r}".format(
                label, self.output_tail(result.stdout), self.output_tail(result.stderr)
            ),
        )

    def command(
        self,
        case: str,
        seed: Optional[int],
        label: str,
        repo: Path,
        expected: int,
        *arguments: str,
        env: Optional[Dict[str, Optional[str]]] = None,
        mechanism: str = "public-cli",
    ) -> subprocess.CompletedProcess:
        before = self.observed(repo)
        result = self.run_cli(*arguments, env=env)
        after = self.observed(repo)
        self.events.append(
            {
                "case": case,
                "seed": seed,
                "label": label,
                "mechanism": mechanism,
                "argv": list(arguments),
                "expected_returncode": expected,
                "returncode": result.returncode,
                "before": before,
                "after": after,
                "stdout_tail": self.output_tail(result.stdout),
                "stderr_tail": self.output_tail(result.stderr),
            }
        )
        self.assert_returncode(result, expected, label)
        if after is not None:
            self.assert_observed_state_invariants(repo)
        return result

    def initialize(
        self,
        case: str,
        seed: Optional[int],
        repo: Path,
        verify: Optional[str],
    ) -> None:
        contract_path = repo / "contract.json"
        self.write_json(contract_path, self.contract())
        arguments = [
            "v2",
            "init",
            "--repo",
            str(repo),
            "--contract-file",
            str(contract_path),
            "--max-cycles",
            "8",
        ]
        if verify is not None:
            arguments.extend(["--verify", verify])
        self.command(case, seed, "init", repo, 0, *arguments)

    def assessment(
        self,
        repo: Path,
        decision: str,
        statuses: Optional[Dict[str, str]] = None,
        next_action: Optional[str] = None,
        blocker: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        state = self.state(repo)
        action_id, _ = self.action(repo)
        criteria = state["contract"]["criteria"]
        statuses = statuses or {criterion["id"]: "unknown" for criterion in criteria}
        return {
            "action_id": action_id,
            "contract_revision": state["contract"]["revision"],
            "decision": decision,
            "criteria": [
                {
                    "id": identifier,
                    "status": status,
                    "evidence": "Observed test artifact for {}.".format(identifier),
                }
                for identifier, status in statuses.items()
            ],
            "next_action": next_action,
            "blocker": blocker,
        }

    def statuses(self, repo: Path, status: str) -> Dict[str, str]:
        return {
            criterion["id"]: status
            for criterion in self.state(repo)["contract"]["criteria"]
        }

    def write_assessment(self, repo: Path, assessment: Dict[str, Any]) -> str:
        action_id, result_path = self.action(repo)
        self.assertEqual(assessment["action_id"], action_id)
        self.write_json(result_path, assessment)
        return action_id

    def submit(
        self,
        case: str,
        seed: Optional[int],
        label: str,
        repo: Path,
        action_id: str,
        expected: int,
        env: Optional[Dict[str, Optional[str]]] = None,
        mechanism: str = "public-cli",
    ) -> subprocess.CompletedProcess:
        return self.command(
            case,
            seed,
            label,
            repo,
            expected,
            "v2",
            "submit",
            "--repo",
            str(repo),
            "--action-id",
            action_id,
            env=env,
            mechanism=mechanism,
        )

    def run_seeded_sequence(self, seed: int) -> None:
        randomizer = random.Random(seed)
        case = "seed-{}".format(seed)
        token = "{}-{:08x}".format(seed, randomizer.randrange(1 << 30))
        resume_kind = ("user_instruction", "condition_observed")[
            randomizer.randrange(2)
        ]
        event_start = len(self.events)
        repo = self.repository(case)
        self.initialize(case, seed, repo, self.counting_verifier())
        first_id, _ = self.action(repo)

        initial = self.durable_snapshot(repo)
        self.command(case, seed, "current-reprint", repo, 0, "v2", "next", "--repo", str(repo))
        self.assertEqual(self.action(repo)[0], first_id)
        self.assert_durable_snapshot_equal(initial, repo, "next must not advance current work")

        invalid = self.assessment(
            repo,
            "complete",
            statuses={"C1": "satisfied"},
        )
        self.write_assessment(repo, invalid)
        before_rejection = self.observed(repo)
        self.submit(case, seed, "current-replay-rejected", repo, first_id, 2)
        self.assertEqual(self.observed(repo)["state_digest"], before_rejection["state_digest"])
        self.assertEqual(self.verifier_count(repo), 0)

        rejected_snapshot = self.durable_snapshot(repo)
        self.command(
            case,
            seed,
            "current-reprint-after-rejection",
            repo,
            0,
            "v2",
            "next",
            "--repo",
            str(repo),
        )
        self.assertEqual(self.action(repo)[0], first_id)
        self.assert_durable_snapshot_equal(
            rejected_snapshot, repo, "reprinting a rejected current action must be stable"
        )

        first_assessment = self.assessment(
            repo,
            "continue",
            next_action="continue {} first action".format(token),
        )
        self.write_assessment(repo, first_assessment)
        self.submit(case, seed, "continue-first", repo, first_id, 0)
        self.assertEqual(self.state(repo)["cycle"], 1)
        self.assertEqual(self.verifier_count(repo), 1)
        second_id, _ = self.action(repo)

        before_old_replay = self.durable_snapshot(repo)
        self.submit(case, seed, "old-replay-after-continue", repo, first_id, 0)
        self.assert_durable_snapshot_equal(
            before_old_replay, repo, "accepted old replay must not consume another cycle"
        )

        original_contract = self.state(repo)["contract"]
        revision_path = repo / "revision.json"
        self.write_json(
            revision_path,
            {
                "base_revision": original_contract["revision"],
                "provenance": {
                    "kind": "user_correction",
                    "reference": "seed {} adds documented behavior".format(seed),
                },
                "interpretation": "Corrected sequence contract {}.".format(token),
                "criteria": list(original_contract["criteria"]) + [self.criterion("C3")],
            },
        )
        count_before_revision = self.verifier_count(repo)
        self.command(
            case,
            seed,
            "revise-after-old-replay",
            repo,
            0,
            "v2",
            "revise",
            "--repo",
            str(repo),
            "--revision-file",
            str(revision_path),
        )
        revised = self.state(repo)
        self.assertEqual(revised["contract"]["revision"], 2)
        self.assertEqual(revised["cycle"], 1)
        self.assertEqual(self.verifier_count(repo), count_before_revision)
        self.assertNotEqual(self.action(repo)[0], second_id)

        stale_snapshot = self.durable_snapshot(repo)
        self.submit(case, seed, "stale-pre-revision-action", repo, second_id, 2)
        self.assert_durable_snapshot_equal(
            stale_snapshot, repo, "stale action cannot consume a revised run"
        )

        blocked_id, _ = self.action(repo)
        blocked = self.assessment(
            repo,
            "blocked",
            blocker={
                "reason": "Fixture needs a resumed authority.",
                "resumption_condition": "Test records the selected authority.",
                "resume_on": resume_kind,
            },
        )
        self.write_assessment(repo, blocked)
        self.submit(case, seed, "pause-after-revision", repo, blocked_id, 0)
        paused = self.state(repo)
        self.assertEqual(paused["phase"], "paused")
        self.assertEqual(paused["cycle"], 1)
        self.assertEqual(self.verifier_count(repo), count_before_revision)

        before_pause_replay = self.durable_snapshot(repo)
        self.submit(case, seed, "old-replay-while-paused", repo, first_id, 0)
        self.assert_durable_snapshot_equal(
            before_pause_replay, repo, "old replay must leave a paused run untouched"
        )

        provenance_path = repo / "resume.json"
        self.write_json(
            provenance_path,
            {
                "provenance": {
                    "kind": resume_kind,
                    "reference": "seed {} observed {}".format(seed, resume_kind),
                }
            },
        )
        self.command(
            case,
            seed,
            "resume-after-pause",
            repo,
            0,
            "v2",
            "resume",
            "--repo",
            str(repo),
            "--provenance-file",
            str(provenance_path),
        )
        resumed_id, _ = self.action(repo)
        self.assertNotEqual(resumed_id, blocked_id)
        self.assertEqual(self.state(repo)["cycle"], 1)
        self.assertEqual(self.verifier_count(repo), count_before_revision)

        resumed_assessment = self.assessment(
            repo,
            "continue",
            next_action="continue {} after resume".format(token),
        )
        self.write_assessment(repo, resumed_assessment)
        self.submit(case, seed, "continue-after-resume", repo, resumed_id, 0)
        self.assertEqual(self.state(repo)["cycle"], 2)
        self.assertEqual(self.verifier_count(repo), 2)

        uncertain_id, _ = self.action(repo)
        uncertain_assessment = self.assessment(
            repo,
            "continue",
            next_action="continue {} after uncertain verifier".format(token),
        )
        self.write_assessment(repo, uncertain_assessment)
        before_uncertainty_history = self.history_bytes(repo)
        self.submit(
            case,
            seed,
            "synthetic-post-verifier-interruption",
            repo,
            uncertain_id,
            70,
            env={"UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER": "1"},
            mechanism="environment-fault-injection",
        )
        uncertain = self.state(repo)
        self.assertEqual(uncertain["cycle"], 2)
        self.assertEqual(uncertain["action"]["id"], uncertain_id)
        self.assertEqual(uncertain["recovery"]["action_id"], uncertain_id)
        self.assertEqual(self.verifier_count(repo), 3)
        self.assertEqual(self.history_bytes(repo), before_uncertainty_history)

        recovery_snapshot = self.durable_snapshot(repo)
        self.command(
            case,
            seed,
            "current-reprint-during-uncertainty",
            repo,
            0,
            "v2",
            "next",
            "--repo",
            str(repo),
        )
        self.assert_durable_snapshot_equal(
            recovery_snapshot, repo, "uncertain verifier state must be recovery-only"
        )

        self.submit(case, seed, "old-replay-during-uncertainty", repo, first_id, 0)
        self.assert_durable_snapshot_equal(
            recovery_snapshot, repo, "old replay cannot resolve verifier uncertainty"
        )
        self.submit(case, seed, "current-submit-during-uncertainty", repo, uncertain_id, 2)
        self.assert_durable_snapshot_equal(
            recovery_snapshot, repo, "current submit cannot rerun an uncertain verifier"
        )

        resolution_path = repo / "resolution.json"
        self.write_json(
            resolution_path,
            {
                "action_id": uncertain_id,
                "provenance": {
                    "kind": "host_observation",
                    "reference": "seed {} inspected synthetic verifier effects".format(seed),
                },
                "resolution": "abandon",
            },
        )
        self.command(
            case,
            seed,
            "resolve-synthetic-uncertainty",
            repo,
            0,
            "v2",
            "resolve-verifier",
            "--repo",
            str(repo),
            "--action-id",
            uncertain_id,
            "--resolution-file",
            str(resolution_path),
        )
        resolved = self.state(repo)
        self.assertEqual(resolved["phase"], "active")
        self.assertEqual(resolved["cycle"], 2)
        self.assertIsNone(resolved["recovery"])
        self.assertNotEqual(self.action(repo)[0], uncertain_id)
        self.assertEqual(self.verifier_count(repo), 3)

        complete_id, _ = self.action(repo)
        complete = self.assessment(
            repo,
            "complete",
            statuses=self.statuses(repo, "satisfied"),
        )
        self.write_assessment(repo, complete)
        self.submit(case, seed, "complete-after-recovery", repo, complete_id, 0)
        done = self.state(repo)
        self.assertEqual(done["phase"], "done")
        self.assertEqual(done["cycle"], 3)
        self.assertEqual(self.verifier_count(repo), 4)

        done_snapshot = self.durable_snapshot(repo)
        self.submit(case, seed, "old-replay-after-done", repo, first_id, 0)
        self.assert_durable_snapshot_equal(
            done_snapshot, repo, "old replay cannot reopen a done run"
        )

        self.case_summaries.append(
            {
                "case": case,
                "seed": seed,
                "random_token": token,
                "resume_kind": resume_kind,
                "event_count": len(self.events) - event_start,
                "final": self.observed(repo),
            }
        )

    @staticmethod
    def pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def kill_process_group(group_id: Optional[int]) -> None:
        if group_id is None:
            return
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait_for(self, predicate: Any, description: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for {}".format(description))

    def record_manual_event(
        self,
        case: str,
        label: str,
        repo: Path,
        details: Dict[str, Any],
    ) -> None:
        self.events.append(
            {
                "case": case,
                "seed": None,
                "label": label,
                "mechanism": "real-subprocess-sigkill",
                "before": self.observed(repo),
                "details": details,
            }
        )

    def run_real_kill_boundary(self) -> None:
        if not hasattr(os, "killpg"):
            self.skipTest("the runtime's POSIX verifier boundary requires killpg")
        case = "real-process-kill"
        event_start = len(self.events)
        repo = self.repository(case)
        started_path = repo / "real-kill-verifier.pid"
        finished_path = repo / "real-kill-verifier.finished"
        verifier = self.python_verify(
            "import os, time\n"
            "from pathlib import Path\n"
            "Path('real-kill-verifier.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
            "while True:\n"
            "    time.sleep(0.05)\n"
            "Path('real-kill-verifier.finished').write_text('finished', encoding='utf-8')\n"
        )
        self.initialize(case, None, repo, verifier)
        action_id, _ = self.action(repo)
        self.write_assessment(
            repo,
            self.assessment(
                repo,
                "continue",
                next_action="Hold the verifier until the parent process is killed.",
            ),
        )

        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "v2",
                "submit",
                "--repo",
                str(repo),
                "--action-id",
                action_id,
            ],
            cwd=str(self.root),
            env=self.command_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        verifier_group: Optional[int] = None
        try:
            self.wait_for(started_path.exists, "the real verifier to start")
            verifier_pid = int(started_path.read_text(encoding="utf-8"))
            verifier_group = os.getpgid(verifier_pid)
            self.wait_for(
                lambda: isinstance(self.state(repo).get("recovery"), dict),
                "the durable verifier uncertainty marker",
            )
            marked = self.state(repo)
            self.assertEqual(marked["recovery"]["action_id"], action_id)
            self.assertTrue(self.pid_exists(verifier_pid))
            self.record_manual_event(
                case,
                "durable-marker-before-real-kill",
                repo,
                {
                    "runtime_process_group": process.pid,
                    "verifier_pid": verifier_pid,
                    "verifier_process_group": verifier_group,
                    "marker_action_id": action_id,
                },
            )

            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            after_kill = self.state(repo)
            self.assertEqual(after_kill["recovery"]["action_id"], action_id)
            self.assertEqual(after_kill["cycle"], 0)
            self.assertTrue(self.pid_exists(verifier_pid))
            self.record_manual_event(
                case,
                "real-runtime-process-group-killed",
                repo,
                {
                    "signal": "SIGKILL",
                    "returncode": process.returncode,
                    "stdout_tail": self.output_tail(stdout),
                    "stderr_tail": self.output_tail(stderr),
                    "external_verifier_still_alive": True,
                },
            )

            recovery_snapshot = self.durable_snapshot(repo)
            self.command(
                case,
                None,
                "next-after-real-kill",
                repo,
                0,
                "v2",
                "next",
                "--repo",
                str(repo),
            )
            self.assert_durable_snapshot_equal(
                recovery_snapshot, repo, "real kill must leave a recovery-only packet"
            )
            self.submit(
                case,
                None,
                "current-submit-after-real-kill",
                repo,
                action_id,
                2,
            )
            self.assert_durable_snapshot_equal(
                recovery_snapshot, repo, "real kill must not permit a verifier retry"
            )

            self.kill_process_group(verifier_group)
            self.record_manual_event(
                case,
                "external-verifier-process-group-killed",
                repo,
                {
                    "signal": "SIGKILL",
                    "verifier_pid": verifier_pid,
                    "verifier_process_group": verifier_group,
                },
            )
            self.assertFalse(finished_path.exists())
            resolution_path = repo / "real-kill-resolution.json"
            self.write_json(
                resolution_path,
                {
                    "action_id": action_id,
                    "provenance": {
                        "kind": "host_observation",
                        "reference": "test observed the durable marker after SIGKILL",
                    },
                    "resolution": "abandon",
                },
            )
            self.command(
                case,
                None,
                "resolve-after-real-kill",
                repo,
                0,
                "v2",
                "resolve-verifier",
                "--repo",
                str(repo),
                "--action-id",
                action_id,
                "--resolution-file",
                str(resolution_path),
            )
            resolved = self.state(repo)
            self.assertEqual(resolved["phase"], "active")
            self.assertEqual(resolved["cycle"], 0)
            self.assertIsNone(resolved["recovery"])
            self.assertNotEqual(self.action(repo)[0], action_id)
        finally:
            if process.poll() is None:
                self.kill_process_group(process.pid)
                process.communicate(timeout=5)
            self.kill_process_group(verifier_group)

        self.case_summaries.append(
            {
                "case": case,
                "seed": None,
                "event_count": len(self.events) - event_start,
                "final": self.observed(repo),
                "fault_mode": "real OS process-group SIGKILL",
            }
        )

    def emit_evidence(self) -> None:
        destination = os.environ.get(EVIDENCE_ENV)
        if not destination:
            return
        path = Path(destination).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "suite": "tests.test_generated_runtime",
            "python": sys.version.split()[0],
            "seeds": list(SEEDS),
            "sequence_count": len(SEEDS),
            "case_count": len(self.case_summaries),
            "event_count": len(self.events),
            "fault_modes": {
                "environment_fault_injection": {
                    "control": "UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER=1",
                    "count": len(SEEDS),
                },
                "real_subprocess_kill": {
                    "control": "SIGKILL sent to the runtime CLI process group",
                    "count": 1,
                },
            },
            "cases": self.case_summaries,
            "events": self.events,
        }
        fd, temporary = tempfile.mkstemp(prefix=".generated-runtime-", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def test_seeded_interactions_and_real_subprocess_kill(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.run_seeded_sequence(seed)
        self.run_real_kill_boundary()
        self.emit_evidence()


if __name__ == "__main__":
    unittest.main()
