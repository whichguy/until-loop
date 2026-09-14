#!/usr/bin/env python3
"""Focused regressions for the Improve factual evidence capture helper."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional, Sequence
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "examples" / "improve" / "scripts" / "capture_evidence.py"
RUNTIME = ROOT / "scripts" / "until-loop"


class ImproveEvidenceCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-evidence-tests-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def environment() -> dict[str, str]:
        return {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def git(self, repo: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(repo),
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result

    def repository(self, name: str, *, commit: bool = True) -> Path:
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "--local", "user.name", "Evidence Fixture")
        self.git(repo, "config", "--local", "user.email", "evidence@example.invalid")
        (repo / "scoped.txt").write_text("base\n", encoding="utf-8")
        if commit:
            self.commit(repo, "seed: first full message", "Historical body one.")
        return repo

    def commit(self, repo: Path, subject: str, body: str) -> None:
        self.git(repo, "add", "--", "scoped.txt")
        self.git(repo, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", subject, "-m", body)

    def initialize_v2(self, repo: Path) -> dict[str, Any]:
        contract = {
            "version": 1,
            "policy": "decision-rubric/2",
            "original_request": "Improve the scoped file.",
            "interpretation": "Inspect the current candidate before assessing it.",
            "criteria": [
                {
                    "id": "C1",
                    "text": "The scoped file is captured.",
                    "basis": {"kind": "request", "reference": "fixture request"},
                }
            ],
        }
        contract_path = repo / "contract.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(RUNTIME),
                "v2",
                "init",
                "--repo",
                str(repo),
                "--contract-file",
                str(contract_path),
            ],
            cwd=str(repo),
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads((repo / ".until-loop" / "state.json").read_text(encoding="utf-8"))

    def capture(self, repo: Path, *, command: str = "snapshot", scopes: Sequence[str] = ("scoped.txt",),
                extra: Sequence[str] = (), expect: int = 0,
                cwd: Optional[Path] = None) -> tuple[subprocess.CompletedProcess[bytes], Optional[Path]]:
        arguments = [
            sys.executable,
            str(HELPER),
            command,
            "--repo",
            str(repo),
            "--owner",
            "standalone-improve",
            "--history-window",
            "7",
        ]
        for scope in scopes:
            arguments.extend(("--scope", scope))
        arguments.extend(extra)
        result = subprocess.run(
            arguments,
            cwd=str(cwd or repo),
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            expect,
            f"stdout={result.stdout.decode('utf-8', 'replace')}\nstderr={result.stderr.decode('utf-8', 'replace')}",
        )
        if expect:
            return result, None
        path = Path(result.stdout.decode("utf-8", "replace").strip())
        self.assertTrue(path.is_file(), result.stdout.decode("utf-8", "replace"))
        return result, path

    @staticmethod
    def record(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def load_helper() -> Any:
        spec = importlib.util.spec_from_file_location("_improve_evidence_test_helper", HELPER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        original = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = original
        return module

    def test_current_stale_missing_and_unbound_artifacts_keep_origins_separate(self) -> None:
        repo = self.repository("binding")
        state = self.initialize_v2(repo)
        _, initial_path = self.capture(repo)
        assert initial_path is not None
        initial = self.record(initial_path)
        candidate = initial["candidate"]
        action = state["action"]
        assert isinstance(action, dict)
        current = self.root / "current-check.json"
        stale = self.root / "stale-reference.md"
        unbound = self.root / "unbound-reference.md"
        current.write_text('{"reported": "anything"}\n', encoding="utf-8")
        stale.write_text("old note\n", encoding="utf-8")
        unbound.write_text("present but not candidate-bound\n", encoding="utf-8")
        missing = self.root / "missing.json"
        current_claim = json.dumps({"candidate": {"digest": candidate["digest"]}, "returncode": 0})
        stale_claim = json.dumps({"candidate": {"digest": "0" * 64}})
        result, path = self.capture(
            repo,
            command="capture",
            extra=(
                "--action-id", action["id"],
                "--contract-revision", str(state["contract"]["revision"]),
                "--reviewer-identity", "fixture-host",
                "--reviewer-role", "independent-reviewer",
                "--reference", str(current),
                "--reference-claim", current_claim,
                "--reference", str(stale),
                "--reference-claim", stale_claim,
                "--reference", str(missing),
                "--reference-claim", current_claim,
                "--reference", str(unbound),
                "--reference-claim", "{}",
                "--check", str(current),
                "--check-claim", current_claim,
                "--check", str(unbound),
                "--check-claim", "{}",
            ),
        )
        self.assertEqual(result.stdout.decode().count("\n"), 1)
        assert path is not None
        record = self.record(path)
        facts = record["tool_facts"]
        self.assertEqual(facts["runtime"]["kind"], "v2")
        self.assertEqual(facts["runtime"]["action_id"], action["id"])
        self.assertEqual(facts["references"][0]["candidate_binding"]["status"], "current")
        self.assertEqual(facts["references"][1]["candidate_binding"]["status"], "stale")
        self.assertEqual(facts["references"][2]["candidate_binding"]["status"], "missing")
        self.assertEqual(facts["references"][3]["candidate_binding"]["status"], "unbound")
        check = facts["checks"][0]
        self.assertEqual(check["candidate_binding"]["status"], "current")
        self.assertEqual(check["returncode"], {"source": "host_declared", "value": 0})
        self.assertNotIn("passed", check)
        self.assertEqual(facts["checks"][1]["candidate_binding"]["status"], "unbound")
        self.assertEqual(record["host_claims"]["reviewer"]["source"], "host_declared")
        self.assertEqual(record["host_claims"]["reviewer"]["role"], "independent-reviewer")
        self.assertEqual(len(record["host_claims"]["references"]), 4)
        self.assertEqual(len(record["host_claims"]["checks"]), 2)

    def test_protocol_action_changes_do_not_stale_the_artifact_digest(self) -> None:
        repo = self.repository("protocol-action")
        state = self.initialize_v2(repo)
        _, first_path = self.capture(repo)
        assert first_path is not None
        first = self.record(first_path)
        action = state["action"]
        assert isinstance(action, dict)
        assessment = {
            "action_id": action["id"],
            "contract_revision": state["contract"]["revision"],
            "decision": "continue",
            "criteria": [
                {
                    "id": "C1",
                    "status": "unknown",
                    "evidence": "No semantic assessment is implied by this fixture.",
                }
            ],
            "next_action": "Continue the fixture review.",
            "blocker": None,
        }
        result_path = Path(action["result_path"])
        result_path.write_text(json.dumps(assessment), encoding="utf-8")
        submitted = subprocess.run(
            [sys.executable, str(RUNTIME), "v2", "submit", "--repo", str(repo), "--action-id", action["id"]],
            cwd=str(repo), env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=15,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr.decode("utf-8", "replace"))
        after_state = json.loads((repo / ".until-loop" / "state.json").read_text(encoding="utf-8"))
        self.assertNotEqual(after_state["action"]["id"], action["id"])

        _, second_path = self.capture(repo, extra=("--prior-record", str(first_path)))
        assert second_path is not None
        second = self.record(second_path)
        self.assertEqual(second["candidate"]["digest"], first["candidate"]["digest"])
        self.assertEqual(second["comparison"]["status"], "unchanged")
        self.assertTrue(second["comparison"]["runtime_binding_changed"])
        self.assertEqual(second["tool_facts"]["runtime"]["action_id"], after_state["action"]["id"])

    def test_same_content_inode_replacement_keeps_candidate_digest(self) -> None:
        repo = self.repository("inode-replacement")
        _, first_path = self.capture(repo)
        assert first_path is not None
        first = self.record(first_path)
        original = repo / "scoped.txt"
        replacement = repo / "replacement.txt"
        replacement.write_bytes(original.read_bytes())
        os.chmod(replacement, stat_mode := os.stat(original).st_mode)
        os.replace(replacement, original)

        _, second_path = self.capture(repo, extra=("--prior-record", str(first_path)))
        assert second_path is not None
        second = self.record(second_path)
        self.assertEqual(second["candidate"]["digest"], first["candidate"]["digest"])
        self.assertEqual(second["comparison"]["status"], "unchanged")
        self.assertNotEqual(
            first["tool_facts"]["scope"][0]["working"]["identity"]["inode"],
            second["tool_facts"]["scope"][0]["working"]["identity"]["inode"],
        )
        self.assertEqual(os.stat(original).st_mode, stat_mode)

    def test_history_catalogue_reuses_full_messages(self) -> None:
        repo = self.repository("history")
        (repo / "scoped.txt").write_text("second\n", encoding="utf-8")
        self.commit(repo, "docs: second full message", "Historical body two with a useful lesson.")
        _, first_path = self.capture(repo)
        _, second_path = self.capture(repo)
        assert first_path is not None and second_path is not None
        first = self.record(first_path)["tool_facts"]["history"]
        second = self.record(second_path)["tool_facts"]["history"]
        self.assertEqual(first["status"], "present")
        self.assertEqual(len(first["window"]), 2)
        self.assertEqual(second["catalogue"]["added_commit_ids"], [])
        self.assertEqual(
            second["catalogue"]["reused_commit_ids"],
            [entry["commit_id"] for entry in second["window"]],
        )
        catalogue = json.loads(Path(first["catalogue"]["path"]).read_text(encoding="utf-8"))
        message = catalogue["commits"][first["window"][0]["commit_id"]]["full_message"]
        self.assertIn("Historical body two", message)

    def test_history_keeps_legal_empty_messages_and_uses_captured_head(self) -> None:
        repo = self.repository("empty-history")
        self.git(
            repo, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "--allow-empty",
            "--allow-empty-message", "-m", "",
        )
        empty_head = self.git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()
        _, path = self.capture(repo)
        assert path is not None
        history = self.record(path)["tool_facts"]["history"]
        self.assertIn(empty_head, [entry["commit_id"] for entry in history["window"]])
        catalogue = json.loads(Path(history["catalogue"]["path"]).read_text(encoding="utf-8"))
        self.assertIsInstance(catalogue["commits"][empty_head]["full_message"], str)

        helper = self.load_helper()
        captured_git = helper.discover_git(repo)
        old_head = captured_git["head"]["commit"]
        (repo / "scoped.txt").write_text("new head\n", encoding="utf-8")
        self.commit(repo, "feat: later head", "This commit must not enter the pinned window.")
        status, entries = helper.git_history(repo, 7, captured_git)
        self.assertEqual(status, "present")
        self.assertEqual(entries[0][0], old_head)
        self.assertNotIn(self.git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip(), [item[0] for item in entries])

    def test_runtime_validation_uses_no_bytecode_cache_when_environment_allows_it(self) -> None:
        repo = self.repository("no-bytecode")
        self.initialize_v2(repo)
        package = self.root / "copied-package"
        copied_helper = package / "examples" / "improve" / "scripts" / "capture_evidence.py"
        copied_runtime = package / "scripts" / "until_loop_v2.py"
        copied_helper.parent.mkdir(parents=True)
        copied_runtime.parent.mkdir(parents=True)
        shutil.copy2(HELPER, copied_helper)
        shutil.copy2(ROOT / "scripts" / "until_loop_v2.py", copied_runtime)
        shutil.copy2(ROOT / "scripts" / "until_loop_packet.py", package / "scripts" / "until_loop_packet.py")
        environment = self.environment()
        environment.pop("PYTHONDONTWRITEBYTECODE")
        result = subprocess.run(
            [
                "/usr/bin/python3", str(copied_helper), "snapshot", "--repo", str(repo),
                "--owner", "standalone-improve", "--history-window", "7", "--scope", "scoped.txt",
            ],
            cwd=str(repo), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertFalse(list((package / "scripts").glob("__pycache__/until_loop_v2*.pyc")))
        self.assertFalse(list((package / "scripts").glob("__pycache__/until_loop_packet*.pyc")))

    def test_scope_records_staged_and_unstaged_identity_without_touching_user_work(self) -> None:
        repo = self.repository("ownership")
        _, before_path = self.capture(repo)
        assert before_path is not None
        (repo / "scoped.txt").write_text("staged\n", encoding="utf-8")
        self.git(repo, "add", "--", "scoped.txt")
        (repo / "scoped.txt").write_text("unstaged\n", encoding="utf-8")
        staged_before = self.git(repo, "diff", "--cached", "--binary", "--", "scoped.txt").stdout
        unstaged_before = self.git(repo, "diff", "--binary", "--", "scoped.txt").stdout
        index_before = self.git(repo, "show", ":scoped.txt").stdout
        working_before = (repo / "scoped.txt").read_bytes()
        _, after_path = self.capture(repo, extra=("--prior-record", str(before_path)))
        assert after_path is not None
        record = self.record(after_path)
        scope = record["tool_facts"]["scope"][0]
        self.assertTrue(scope["staged"]["changed"])
        self.assertTrue(scope["unstaged"]["changed"])
        self.assertEqual(scope["index"]["status"], "present")
        self.assertEqual(record["comparison"]["status"], "drifted")
        self.assertIn("scope", record["comparison"]["changed_components"])
        self.assertEqual(self.git(repo, "diff", "--cached", "--binary", "--", "scoped.txt").stdout, staged_before)
        self.assertEqual(self.git(repo, "diff", "--binary", "--", "scoped.txt").stdout, unstaged_before)
        self.assertEqual(self.git(repo, "show", ":scoped.txt").stdout, index_before)
        self.assertEqual((repo / "scoped.txt").read_bytes(), working_before)

    def test_capture_rejects_scope_or_runtime_drift_before_writing_records(self) -> None:
        repo = self.repository("capture-drift")
        helper = self.load_helper()
        arguments = helper.build_parser().parse_args(
            [
                "snapshot", "--repo", str(repo), "--owner", "standalone-improve",
                "--history-window", "7", "--scope", "scoped.txt",
            ]
        )
        original = helper.collect_artifacts
        changed = False

        def mutate_scope(*args: Any, **kwargs: Any) -> Any:
            nonlocal changed
            if not changed:
                changed = True
                (repo / "scoped.txt").write_text("changed during capture\n", encoding="utf-8")
            return original(*args, **kwargs)

        with mock.patch.object(helper, "collect_artifacts", side_effect=mutate_scope):
            with self.assertRaisesRegex(helper.EvidenceCaptureError, "candidate changed during capture"):
                helper.capture(arguments)
        evidence = repo / ".until-loop" / "evidence"
        self.assertTrue(evidence.is_dir())
        self.assertEqual(list(evidence.iterdir()), [])

        runtime_repo = self.repository("runtime-drift")
        state = self.initialize_v2(runtime_repo)
        action = state["action"]
        assert isinstance(action, dict)
        assessment = {
            "action_id": action["id"],
            "contract_revision": state["contract"]["revision"],
            "decision": "continue",
            "criteria": [{"id": "C1", "status": "unknown", "evidence": "Fixture."}],
            "next_action": "Continue.",
            "blocker": None,
        }
        Path(action["result_path"]).write_text(json.dumps(assessment), encoding="utf-8")
        runtime_arguments = helper.build_parser().parse_args(
            [
                "snapshot", "--repo", str(runtime_repo), "--owner", "standalone-improve",
                "--history-window", "7", "--scope", "scoped.txt",
            ]
        )
        advanced = False

        def advance_runtime(*args: Any, **kwargs: Any) -> Any:
            nonlocal advanced
            if not advanced:
                advanced = True
                result = subprocess.run(
                    [
                        sys.executable, str(RUNTIME), "v2", "submit", "--repo", str(runtime_repo),
                        "--action-id", action["id"],
                    ],
                    cwd=str(runtime_repo), env=self.environment(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            return original(*args, **kwargs)

        with mock.patch.object(helper, "collect_artifacts", side_effect=advance_runtime):
            with self.assertRaisesRegex(helper.EvidenceCaptureError, "runtime binding"):
                helper.capture(runtime_arguments)
        runtime_evidence = runtime_repo / ".until-loop" / "evidence"
        self.assertTrue(runtime_evidence.is_dir())
        self.assertEqual(list(runtime_evidence.iterdir()), [])

    def test_relative_prior_record_is_repo_relative_from_other_cwd_and_cannot_escape(self) -> None:
        repo = self.repository("relative-prior")
        _, first_path = self.capture(repo)
        assert first_path is not None
        relative = str(first_path.relative_to(repo.resolve()))
        _, second_path = self.capture(
            repo, cwd=self.root, extra=("--prior-record", relative)
        )
        assert second_path is not None
        self.assertEqual(self.record(second_path)["comparison"]["status"], "unchanged")
        result, _ = self.capture(
            repo, cwd=self.root, extra=("--prior-record", "../outside.json"), expect=2
        )
        self.assertIn("--prior-record must remain", result.stderr.decode("utf-8", "replace"))

    def test_invalid_prior_does_not_persist_history_catalogue_or_record(self) -> None:
        repo = self.repository("invalid-prior")
        evidence = repo.resolve() / ".until-loop" / "evidence"
        evidence.mkdir(parents=True)
        invalid = evidence / "invalid-prior.json"
        invalid.write_text("{}\n", encoding="utf-8")

        result, _ = self.capture(
            repo,
            extra=("--prior-record", str(invalid.relative_to(repo.resolve()))),
            expect=2,
        )

        self.assertIn("unsupported prior evidence record", result.stderr.decode("utf-8", "replace"))
        self.assertEqual([path.name for path in evidence.iterdir()], ["invalid-prior.json"])

    def test_unborn_history_and_v1_state_are_explicit(self) -> None:
        unborn = self.repository("unborn", commit=False)
        _, unborn_path = self.capture(unborn)
        assert unborn_path is not None
        unborn_record = self.record(unborn_path)
        self.assertEqual(unborn_record["tool_facts"]["git"]["head"]["status"], "absent")
        self.assertEqual(unborn_record["tool_facts"]["history"]["status"], "absent")
        self.assertEqual(unborn_record["tool_facts"]["history"]["window"], [])

        v1 = self.repository("v1")
        run_dir = v1 / ".until-loop"
        run_dir.mkdir()
        state = {
            "version": 1,
            "phase": "active",
            "objective": "fixture",
            "done_when": "fixture is recorded",
            "verify_cmd": None,
            "max_cycles": 2,
            "cycle": 0,
            "repo_root": str(v1.resolve()),
            "last_evidence": None,
            "last_verify": None,
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        _, v1_path = self.capture(v1)
        assert v1_path is not None
        runtime = self.record(v1_path)["tool_facts"]["runtime"]
        self.assertEqual(runtime["kind"], "v1")
        self.assertIsNone(runtime["action_id"])
        self.assertIsNone(runtime["contract_revision"])

    def test_unsafe_evidence_paths_and_scope_are_rejected(self) -> None:
        repo = self.repository("unsafe")
        outside = self.root / "outside"
        outside.mkdir()
        run_dir = repo / ".until-loop"
        run_dir.mkdir()
        (run_dir / "evidence").symlink_to(outside, target_is_directory=True)
        result, _ = self.capture(repo, expect=2)
        self.assertIn("unsafe default evidence directory", result.stderr.decode("utf-8", "replace"))
        self.assertEqual(list(outside.iterdir()), [])

        paths = self.repository("bad-scope")
        result, _ = self.capture(paths, scopes=("../outside",), expect=2)
        self.assertIn("unsafe scope path", result.stderr.decode("utf-8", "replace"))

        multi = self.repository("multilink")
        evidence = multi / ".until-loop" / "evidence"
        evidence.mkdir(parents=True)
        catalogue = evidence / "history-catalogue.json"
        catalogue.write_text('{"commits": {}, "format": "until-loop-improve-history-catalogue/v1"}\n', encoding="utf-8")
        os.link(catalogue, evidence / "catalogue-alias.json")
        result, _ = self.capture(multi, expect=2)
        self.assertIn("unsafe history catalogue", result.stderr.decode("utf-8", "replace"))

    def test_directory_and_incomplete_scope_cannot_establish_current_binding(self) -> None:
        directory = self.repository("directory-scope")
        (directory / "scope-dir").mkdir()
        result, _ = self.capture(directory, scopes=("scope-dir",), expect=2)
        self.assertIn("--scope must name a file", result.stderr.decode("utf-8", "replace"))

        repo = self.repository("incomplete-scope")
        helper = self.load_helper()
        arguments = helper.build_parser().parse_args(
            [
                "snapshot", "--repo", str(repo), "--owner", "standalone-improve",
                "--history-window", "7", "--scope", "scoped.txt",
            ]
        )
        original = helper.observed_file

        def too_large(path: Path) -> dict[str, Any]:
            if path.resolve() == (repo / "scoped.txt").resolve():
                return {"status": "too_large", "limit": 1}
            return original(path)

        with mock.patch.object(helper, "observed_file", side_effect=too_large):
            record_path = helper.capture(arguments)
        record = self.record(record_path)
        self.assertFalse(record["candidate"]["complete"])
        self.assertIn("scoped.txt:working", record["candidate"]["incomplete"])
        binding = helper.candidate_binding(
            {"status": "present"},
            {"candidate": {"digest": record["candidate"]["digest"]}},
            record["candidate"],
        )
        self.assertEqual(binding["status"], "unbound")
        self.assertIn("incomplete", binding["reason"])

    def test_mismatched_v2_action_is_rejected(self) -> None:
        repo = self.repository("action")
        self.initialize_v2(repo)
        result, _ = self.capture(repo, extra=("--action-id", "0" * 32), expect=2)
        self.assertIn("does not match the current v2 action", result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
