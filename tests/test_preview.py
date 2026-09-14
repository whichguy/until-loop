#!/usr/bin/env python3
"""Read-only preview regressions for the until-loop v2 contract boundary.

These tests compose the established v2 fixture helpers rather than inherit
their test case, so discovery does not run the protocol suite twice.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_v2 as _test_v2


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = _test_v2.UntilLoopV2ProtocolTests()
        self.protocol.setUp()

    def tearDown(self) -> None:
        try:
            self.protocol.doCleanups()
        finally:
            self.protocol.tearDown()

    def preview(
        self,
        contract_file: str,
        *,
        input_bytes: bytes | None = None,
        extra: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.public_preview(
            _test_v2.CLI,
            contract_file,
            cwd=cwd or self.protocol.root,
            input_bytes=input_bytes,
            extra=extra,
            suppress_bytecode=True,
        )

    def public_preview(
        self,
        cli: Path,
        contract_file: str,
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        extra: tuple[str, ...] = (),
        suppress_bytecode: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "LANG": "C",
        }
        if suppress_bytecode:
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(cli),
                "v2",
                "preview",
                "--contract-file",
                contract_file,
                *extra,
            ],
            cwd=str(cwd),
            env=environment,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

    def write_contract(self, name: str, value: object) -> Path:
        path = self.protocol.root / name
        self.protocol.write_json(path, value)
        return path

    def copied_preview_source(self) -> Path:
        copied = self.protocol.root / "fresh-preview-skill"
        for relative in (
            Path("scripts/until-loop"),
            Path("scripts/until_loop_v2.py"),
            Path("scripts/until_loop_packet.py"),
            Path("references/decision-rubric.md"),
            Path("examples/improve/references/review-policy.md"),
        ):
            target = copied / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_test_v2.SKILL_ROOT / relative, target)
        return copied

    @staticmethod
    def preview_json(result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
        return json.loads(result.stdout.decode("utf-8"))

    def assert_no_run_artifacts(self) -> None:
        self.assertFalse((self.protocol.root / ".until-loop").exists())

    def test_installed_parent_and_runtime_links_share_read_only_preview(self) -> None:
        source = self.copied_preview_source()
        for relative in ("SKILL.md", "examples/improve/SKILL.md"):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_test_v2.SKILL_ROOT / relative, target)
        host = self.protocol.root / "host skills"
        host.mkdir()
        for name, target in (("until-loop", source), ("improve", source / "examples/improve")):
            (host / name).symlink_to(os.path.relpath(target, host), target_is_directory=True)
        physical_parent = (host / "improve/SKILL.md").resolve()
        bound_card = (physical_parent.parent / "../../SKILL.md").resolve()
        self.assertEqual(bound_card, (host / "until-loop/SKILL.md").resolve())
        bound_policy = physical_parent.parent / "references/review-policy.md"
        self.assertTrue(bound_policy.is_file())
        self.assertEqual(
            bound_policy.read_bytes(),
            (host / "until-loop/examples/improve/references/review-policy.md").read_bytes(),
        )
        before = self.protocol.tree_snapshot(self.protocol.root)
        result = self.public_preview(
            host / "until-loop/scripts/until-loop", "-", cwd=host,
            input_bytes=json.dumps(self.protocol.contract()).encode("utf-8"),
            suppress_bytecode=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        response = self.preview_json(result)
        self.assertEqual(response["mode"], "preview")
        self.assertEqual(response["status"], "not_initialized")
        self.assertEqual(response["execution"], "not_executed")
        self.assertEqual(self.protocol.tree_snapshot(self.protocol.root), before)

    def test_file_preview_is_stable_outside_git_and_does_not_initialize(self) -> None:
        self.assertFalse((self.protocol.root / ".git").exists())
        contract_path = self.write_contract("preview-contract.json", self.protocol.contract())
        before = self.protocol.tree_snapshot(self.protocol.root)

        first = self.preview(str(contract_path))
        second = self.preview(str(contract_path))

        self.protocol.assert_returncode(first, 0)
        self.protocol.assert_returncode(second, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(self.protocol.tree_snapshot(self.protocol.root), before)
        self.assert_no_run_artifacts()
        payload = self.preview_json(first)
        expected_contract = self.protocol.contract()
        expected_contract["revision"] = 1
        self.assertEqual(payload["mode"], "preview")
        self.assertEqual(payload["status"], "not_initialized")
        self.assertEqual(payload["execution"], "not_executed")
        self.assertEqual(payload["contract"], expected_contract)
        self.assertEqual(
            payload["contract_digest"],
            self.protocol.canonical_state_digest(expected_contract),
        )
        self.assertIsInstance(payload["policy_snapshot"], dict)
        self.assertEqual(
            payload["policy_snapshot"]["version"],
            expected_contract["policy"],
        )
        self.assertNotIn("action", payload)
        self.assertNotIn("until_loop_v2.py", first.stdout.decode("utf-8"))

    def test_preview_contract_and_policy_match_init_normalization(self) -> None:
        contract = self.protocol.contract()
        preview_path = self.write_contract("equal-preview-contract.json", contract)

        preview = self.preview(str(preview_path))
        self.protocol.assert_returncode(preview, 0)
        preview_payload = self.preview_json(preview)

        repo = self.protocol.repository("normalization-equality")
        self.protocol.initialize(repo, contract=contract)
        state = self.protocol.state(repo)

        self.assertEqual(preview_payload["contract"], state["contract"])
        self.assertEqual(preview_payload["policy_snapshot"], state["policy_snapshot"])
        self.assertEqual(
            preview_payload["contract_digest"],
            self.protocol.canonical_state_digest(state["contract"]),
        )

    def test_stdin_preview_is_bounded_and_matches_file_preview_without_writes(self) -> None:
        contract = self.protocol.contract()
        raw = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
        before = self.protocol.tree_snapshot(self.protocol.root)

        from_stdin = self.preview("-", input_bytes=raw)

        self.protocol.assert_returncode(from_stdin, 0)
        self.assertEqual(self.protocol.tree_snapshot(self.protocol.root), before)
        self.assert_no_run_artifacts()
        contract_path = self.write_contract("stdin-comparison.json", contract)
        from_file = self.preview(str(contract_path))
        self.protocol.assert_returncode(from_file, 0)
        self.assertEqual(from_stdin.stdout, from_file.stdout)

    def test_init_preserves_literal_dash_contract_file_without_reading_stdin(self) -> None:
        repo = self.protocol.repository("literal-dash-contract")
        self.protocol.write_json(repo / "-", self.protocol.contract())
        result = subprocess.run(
            [
                sys.executable,
                str(_test_v2.CLI),
                "v2",
                "init",
                "--repo",
                str(repo),
                "--contract-file",
                "-",
            ],
            cwd=str(repo),
            env={
                "PATH": os.defpath,
                "LC_ALL": "C",
                "LANG": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            input=b"{not valid JSON from stdin}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.protocol.assert_returncode(result, 0)
        self.assertEqual(self.protocol.state(repo)["contract"]["revision"], 1)

    def test_init_missing_literal_dash_rejects_without_reading_stdin(self) -> None:
        runtime = self.protocol.load_v2_runtime()
        repo = self.protocol.repository("missing-literal-dash-contract")
        stderr = io.StringIO()
        with (
            mock.patch.object(runtime, "read_stdin_record", side_effect=AssertionError("stdin read")),
            mock.patch.object(runtime, "with_lock", side_effect=AssertionError("lock acquired")),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = runtime.main(
                ["init", "--repo", str(repo), "--contract-file", "-"]
            )

        self.assertEqual(exit_code, 2, stderr.getvalue())
        self.assertIn("missing contract file", stderr.getvalue())
        self.assertFalse(self.protocol.run_dir(repo).exists())

    def test_public_preview_without_bytecode_flag_keeps_source_and_cwd_unchanged(self) -> None:
        source = self.copied_preview_source()
        workspace = self.protocol.root / "public-preview-workspace"
        workspace.mkdir()
        contract_path = workspace / "contract.json"
        self.protocol.write_json(contract_path, self.protocol.contract())
        source_before = self.protocol.tree_snapshot(source)
        workspace_before = self.protocol.tree_snapshot(workspace)

        result = self.public_preview(
            source / "scripts" / "until-loop",
            str(contract_path),
            cwd=workspace,
            suppress_bytecode=False,
        )

        self.protocol.assert_returncode(result, 0)
        self.assertEqual(self.protocol.tree_snapshot(source), source_before)
        self.assertEqual(self.protocol.tree_snapshot(workspace), workspace_before)
        self.assertFalse((source / "scripts" / "__pycache__").exists())
        self.assert_no_run_artifacts()

    def test_preview_rejects_malformed_policy_schema_duplicates_and_oversize(self) -> None:
        invalid = {
            "malformed-json": b"{",
            "policy-mismatch": json.dumps(
                self.protocol.contract(policy="decision-rubric/not-loaded")
            ).encode("utf-8"),
            "unsupported-schema": json.dumps(
                self.protocol.contract(extra="not allowed")
            ).encode("utf-8"),
            "duplicate-criteria": json.dumps(
                self.protocol.contract(
                    [self.protocol.criterion("C1"), self.protocol.criterion("C1")]
                )
            ).encode("utf-8"),
            "oversized-stdin": b"x" * (64 * 1024 + 1),
        }
        for name, raw in invalid.items():
            with self.subTest(name=name):
                if name == "oversized-stdin":
                    result = self.preview("-", input_bytes=raw)
                else:
                    path = self.protocol.root / f"{name}.json"
                    path.write_bytes(raw)
                    result = self.preview(str(path))
                self.protocol.assert_returncode(result, 2)
                self.assertEqual(result.stdout, b"")
                self.assert_no_run_artifacts()

    def test_documented_git_diff_recipe_preserves_stale_index(self) -> None:
        """A successful read-only preview also depends on read-only discovery."""
        repo = self.protocol.repository("stale index fixture")
        env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}

        def git(*args: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(["git", "-C", str(repo), *args], env=env,
                                  capture_output=True, check=True, timeout=10)

        git("init", "-q")
        tracked = repo / "tracked.txt"
        tracked.write_text("unchanged content\n", encoding="utf-8")
        git("add", "--", "tracked.txt")
        stamp = tracked.stat()
        os.utime(tracked, (stamp.st_atime, stamp.st_mtime + 5))
        before = self.protocol.tree_snapshot(repo)
        recipe = next(line for line in
                      (_test_v2.SKILL_ROOT / "references/runtime-v2.md").read_text().splitlines()
                      if line.startswith("git ") and ' -C "$REPO" diff ' in line)
        argv = [str(repo) if token == "$REPO" else token for token in shlex.split(recipe)]
        result = subprocess.run(argv, env=env, capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(self.protocol.tree_snapshot(repo), before)

    def test_preview_rejects_unsafe_contract_links(self) -> None:
        source = self.write_contract("safe-contract.json", self.protocol.contract())
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                linked = self.protocol.root / f"{kind}-contract.json"
                if kind == "symlink":
                    linked.symlink_to(source)
                else:
                    os.link(source, linked)
                result = self.preview(str(linked))
                self.protocol.assert_returncode(result, 2)
                self.assertEqual(result.stdout, b"")
                self.assert_no_run_artifacts()

    def test_preview_parser_has_no_repo_or_verifier_options(self) -> None:
        contract_path = self.write_contract("parser-contract.json", self.protocol.contract())
        for option in (("--repo", str(self.protocol.root)), ("--verify", "false")):
            with self.subTest(option=option[0]):
                result = self.preview(str(contract_path), extra=option)
                self.protocol.assert_returncode(result, 64)
                self.assert_no_run_artifacts()

    def test_preview_leaves_existing_lock_and_recovery_bytes_unchanged(self) -> None:
        repo = self.protocol.repository("existing-recovery")
        self.protocol.initialize(repo, verify=self.protocol.counting_verifier())
        action_id, _ = self.protocol.action(repo)
        self.protocol.write_assessment(
            repo,
            self.protocol.assessment(
                repo,
                decision="continue",
                next_action="Inspect the unresolved criterion.",
            ),
        )
        crashed = self.protocol.run_cli(
            "v2",
            "submit",
            "--repo",
            str(repo),
            "--action-id",
            action_id,
            env={"UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER": "1"},
        )
        self.protocol.assert_returncode(crashed, 70)
        run_dir = self.protocol.run_dir(repo)
        before = self.protocol.tree_snapshot(run_dir)
        verifier_count = self.protocol.verifier_count(repo)
        recovery_before = self.protocol.state(repo)["recovery"]
        self.assertIsNotNone(recovery_before)
        contract_path = self.write_contract("unchanged-contract.json", self.protocol.contract())

        preview = self.preview(str(contract_path), cwd=repo)

        self.protocol.assert_returncode(preview, 0)
        self.assertEqual(self.protocol.tree_snapshot(run_dir), before)
        self.assertEqual(self.protocol.state(repo)["recovery"], recovery_before)
        self.assertEqual(self.protocol.verifier_count(repo), verifier_count)
        self.assert_no_run_artifacts()

    def test_preview_bypasses_repo_lock_random_action_and_environment_verifier_paths(self) -> None:
        runtime = self.protocol.load_v2_runtime()
        contract_path = self.write_contract("direct-preview-contract.json", self.protocol.contract())
        before = self.protocol.tree_snapshot(self.protocol.root)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "dont_write_bytecode", False),
            mock.patch.object(runtime, "legacy_runtime", side_effect=AssertionError("repo path used")),
            mock.patch.object(runtime, "with_lock", side_effect=AssertionError("lock path used")),
            mock.patch.object(runtime.os, "urandom", side_effect=AssertionError("action id created")),
            mock.patch.dict(
                os.environ,
                {"UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER": "1", "UNTIL_LOOP_MAX_CYCLES": "1"},
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = runtime.main(["preview", "--contract-file", str(contract_path)])
            self.assertFalse(sys.dont_write_bytecode)

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn('"mode": "preview"', stdout.getvalue())
        self.assertEqual(self.protocol.tree_snapshot(self.protocol.root), before)
        self.assert_no_run_artifacts()


if __name__ == "__main__":
    unittest.main()
