#!/usr/bin/env python3
"""Hermetic behavioral regressions for the until-loop runtime."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLI = SKILL_ROOT / "scripts" / "until-loop"
RUN_DIRNAME = ".until-loop"


class UntilLoopRuntimeTests(unittest.TestCase):
    """Exercise durable CLI behavior through isolated temporary repositories."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="until-loop-runtime-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_cli(
        self,
        *arguments: str,
        cwd: Path | None = None,
        env: dict[str, str | None] | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[bytes]:
        command_env = os.environ.copy()
        if env:
            for key, value in env.items():
                if value is None:
                    command_env.pop(key, None)
                else:
                    command_env[key] = value
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=str(cwd or self.root),
            env=command_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    def repository(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True)
        return repo

    def python_verify(self, code: str) -> str:
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    def assert_returncode(
        self, result: subprocess.CompletedProcess[bytes], expected: int
    ) -> None:
        stdout = result.stdout.decode("utf-8", errors="replace")[-2_000:]
        stderr = result.stderr.decode("utf-8", errors="replace")[-2_000:]
        self.assertEqual(
            result.returncode,
            expected,
            f"stdout tail={stdout!r}\nstderr tail={stderr!r}",
        )

    def initialize(
        self,
        repo: Path,
        *,
        prompt: str = "objective",
        done_when: str | None = None,
        verify: str | None = None,
        max_cycles: int | None = None,
        force: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        arguments = ["init", "--repo", str(repo), "--prompt", prompt]
        if done_when is not None:
            arguments.extend(["--done-when", done_when])
        if verify is not None:
            arguments.extend(["--verify", verify])
        if max_cycles is not None:
            arguments.extend(["--max-cycles", str(max_cycles)])
        if force:
            arguments.append("--force")
        result = self.run_cli(*arguments)
        self.assert_returncode(result, 0)
        return result

    def complete(
        self, repo: Path, evidence: str, *, done: bool = False, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        arguments = ["complete", "--repo", str(repo), "--evidence", evidence]
        if done:
            arguments.append("--done")
        return self.run_cli(*arguments, **kwargs)

    @staticmethod
    def run_dir(repo: Path) -> Path:
        return repo / RUN_DIRNAME

    def state_path(self, repo: Path) -> Path:
        return self.run_dir(repo) / "state.json"

    def history_path(self, repo: Path) -> Path:
        return self.run_dir(repo) / "history.jsonl"

    def read_state(self, repo: Path) -> dict[str, object]:
        return json.loads(self.state_path(repo).read_text(encoding="utf-8"))

    def read_history(self, repo: Path) -> list[dict[str, object]]:
        text = self.history_path(repo).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def state_and_history_bytes(self, repo: Path) -> tuple[bytes, bytes]:
        return self.state_path(repo).read_bytes(), self.history_path(repo).read_bytes()

    def run_parallel(self, argument_lists: list[list[str]]) -> list[subprocess.CompletedProcess[bytes]]:
        start = threading.Barrier(len(argument_lists) + 1)
        results: list[subprocess.CompletedProcess[bytes] | None] = [None] * len(argument_lists)
        errors: list[BaseException] = []

        def worker(index: int, arguments: list[str]) -> None:
            try:
                start.wait(timeout=5)
                results[index] = self.run_cli(*arguments, timeout=15)
            except BaseException as exc:  # propagated after all workers join
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index, arguments), daemon=True)
            for index, arguments in enumerate(argument_lists)
        ]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(errors, f"parallel CLI failure: {errors!r}")
        self.assertFalse(any(thread.is_alive() for thread in threads), "parallel CLI timed out")
        if any(result is None for result in results):
            self.fail("parallel CLI did not return a result")
        return [result for result in results if result is not None]

    @staticmethod
    def tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
        snapshot: dict[str, tuple[str, bytes | str | None]] = {}
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_dir():
                snapshot[relative] = ("dir", None)
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes())
        return snapshot

    def load_runtime_module(self) -> object:
        name = f"until_loop_runtime_test_{id(self)}"
        loader = SourceFileLoader(name, str(CLI))
        spec = importlib.util.spec_from_loader(name, loader)
        if spec is None:
            self.fail("could not load until-loop runtime")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        loader.exec_module(module)
        return module

    def test_whitespace_evidence_is_usage_and_keeps_state_and_history(self) -> None:
        repo = self.repository("whitespace-evidence")
        self.initialize(repo)
        before = self.state_and_history_bytes(repo)

        result = self.complete(repo, " \t\n ")

        self.assert_returncode(result, 64)
        self.assertEqual(self.state_and_history_bytes(repo), before)

    def test_blank_init_options_and_invalid_max_do_not_create_run_artifacts(self) -> None:
        cases = {
            "blank-done-when": ["--done-when", " \t\n"],
            "blank-verify": ["--verify", "  "],
            "zero-max": ["--max-cycles", "0"],
            "negative-max": ["--max-cycles", "-1"],
            "nonnumeric-max": ["--max-cycles", "not-a-number"],
        }
        for name, extra in cases.items():
            with self.subTest(name=name):
                repo = self.repository(name)
                result = self.run_cli(
                    "init", "--repo", str(repo), "--prompt", "objective", *extra
                )
                self.assert_returncode(result, 64)
                self.assertFalse(self.run_dir(repo).exists())

    def test_equals_form_preserves_option_looking_values(self) -> None:
        repo = self.repository("equals-option-values")

        initialized = self.run_cli(
            "init",
            "--repo",
            str(repo),
            "--prompt=--help",
            "--done-when=--done",
        )

        self.assert_returncode(initialized, 0)
        state = self.read_state(repo)
        self.assertEqual(state["objective"], "--help")
        self.assertEqual(state["done_when"], "--done")

        completed = self.run_cli(
            "complete", "--repo", str(repo), "--evidence=--help"
        )

        self.assert_returncode(completed, 0)
        self.assertEqual(self.read_state(repo)["last_evidence"], "--help")

        rejected_cases = (
            ("init", "--repo", str(self.repository("separate-prompt")), "--prompt", "--done-when"),
            ("init", "--repo", str(self.repository("separate-done-when")), "--prompt", "objective", "--done-when", "--done"),
            ("complete", "--repo", str(repo), "--evidence", "--done"),
        )
        for arguments in rejected_cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assert_returncode(result, 64)

    def test_invalid_states_are_rejected_before_verification_or_mutation(self) -> None:
        cases = ("malformed", "unknown-version", "unknown-phase", "blocked", "type-invalid")
        verifier = self.python_verify(
            "from pathlib import Path; Path('verifier-ran').write_text('ran')"
        )
        for name in cases:
            with self.subTest(name=name):
                repo = self.repository(f"invalid-state-{name}")
                self.initialize(repo, verify=verifier)
                state_path = self.state_path(repo)
                if name == "malformed":
                    state_path.write_bytes(b"{ definitely not JSON")
                else:
                    state = self.read_state(repo)
                    if name == "unknown-version":
                        state["version"] = 99
                    elif name == "unknown-phase":
                        state["phase"] = "mystery"
                    elif name == "blocked":
                        state["phase"] = "blocked"
                    else:
                        state["cycle"] = "0"
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                before = self.state_and_history_bytes(repo)

                result = self.complete(repo, "evidence", done=True)

                self.assert_returncode(result, 2)
                self.assertEqual(self.state_and_history_bytes(repo), before)
                self.assertFalse((repo / "verifier-ran").exists())

    def test_saved_repo_root_mismatch_never_runs_verifier_elsewhere(self) -> None:
        repo = self.repository("selected-repo")
        other = self.repository("other-repo")
        verifier = self.python_verify(
            "from pathlib import Path; Path('verifier-ran').write_text('ran')"
        )
        self.initialize(repo, verify=verifier)
        state = self.read_state(repo)
        state["repo_root"] = str(other.resolve())
        self.state_path(repo).write_text(json.dumps(state), encoding="utf-8")
        before = self.state_and_history_bytes(repo)

        result = self.complete(repo, "evidence", done=True)

        self.assert_returncode(result, 2)
        self.assertEqual(self.state_and_history_bytes(repo), before)
        self.assertFalse((other / "verifier-ran").exists())

    def test_repo_name_spaces_are_preserved(self) -> None:
        repo = self.repository("  named with spaces  ")
        stripped_sibling = self.repository("named with spaces")

        self.initialize(repo)

        state = self.read_state(repo)
        self.assertEqual(state["repo_root"], str(repo.resolve()))
        self.assertTrue(self.run_dir(repo).is_dir())
        self.assertFalse(self.run_dir(stripped_sibling).exists())

    def test_nonfinite_and_negative_verify_timeouts_fall_back_to_a_usable_default(self) -> None:
        verify = self.python_verify("import time; time.sleep(0.05)")
        for raw_timeout in ("inf", "nan", "1e309", "-1"):
            with self.subTest(raw_timeout=raw_timeout):
                repo = self.repository(f"timeout-{raw_timeout}")
                self.initialize(repo, verify=verify)
                result = self.complete(
                    repo,
                    "finished",
                    done=True,
                    env={"UNTIL_LOOP_VERIFY_TIMEOUT": raw_timeout},
                )
                self.assert_returncode(result, 0)
                state = self.read_state(repo)
                self.assertEqual(state["phase"], "done")
                self.assertEqual((state["last_verify"] or {})["exit"], 0)

    def test_invalid_utf8_verifier_output_is_replaced_and_can_complete(self) -> None:
        verify = self.python_verify(
            "import sys; sys.stdout.buffer.write(b'good\\xff\\n'); "
            "sys.stderr.buffer.write(b'bad\\xfe\\n')"
        )
        repo = self.repository("invalid-utf8")
        self.initialize(repo, verify=verify)

        result = self.complete(repo, "finished", done=True)

        self.assert_returncode(result, 0)
        state = self.read_state(repo)
        verify_result = state["last_verify"] or {}
        self.assertEqual(state["phase"], "done")
        self.assertIs(verify_result["ok"], True)
        self.assertEqual(verify_result["exit"], 0)
        self.assertIn("\ufffd", verify_result["tail"])
        self.assertIn("\ufffd", result.stdout.decode("utf-8", errors="replace"))

    def test_duplicate_parallel_done_consumes_only_one_completion(self) -> None:
        repo = self.repository("parallel-done")
        self.initialize(
            repo,
            verify=self.python_verify("import time; time.sleep(0.15)"),
        )
        arguments = [
            ["complete", "--repo", str(repo), "--evidence", "finished", "--done"],
            ["complete", "--repo", str(repo), "--evidence", "finished", "--done"],
        ]

        results = self.run_parallel(arguments)

        self.assertEqual(sorted(result.returncode for result in results), [0, 2])
        state = self.read_state(repo)
        history = self.read_history(repo)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["cycle"], 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["cycle"], 1)

    def test_concurrent_completes_have_unique_cycles_and_history(self) -> None:
        repo = self.repository("parallel-normal")
        workers = 4
        self.initialize(
            repo,
            max_cycles=workers + 2,
            verify=self.python_verify("import time; time.sleep(0.08)"),
        )
        arguments = [
            ["complete", "--repo", str(repo), "--evidence", f"increment-{index}"]
            for index in range(workers)
        ]

        results = self.run_parallel(arguments)

        for result in results:
            self.assert_returncode(result, 0)
        state = self.read_state(repo)
        history = self.read_history(repo)
        cycles = [event["cycle"] for event in history]
        self.assertEqual(state["phase"], "active")
        self.assertEqual(state["cycle"], workers)
        self.assertEqual(sorted(cycles), list(range(1, workers + 1)))
        self.assertEqual(len(set(cycles)), workers)

    def test_terminal_states_refuse_completion_without_mutating_metadata(self) -> None:
        for terminal in ("done", "halted"):
            with self.subTest(terminal=terminal):
                repo = self.repository(f"terminal-{terminal}")
                if terminal == "done":
                    self.initialize(repo)
                    self.assert_returncode(self.complete(repo, "finished", done=True), 0)
                else:
                    self.initialize(repo, max_cycles=1)
                    self.assert_returncode(self.complete(repo, "increment"), 0)
                before = self.state_and_history_bytes(repo)

                result = self.complete(repo, "late update", done=True)

                self.assert_returncode(result, 2)
                self.assertEqual(self.state_and_history_bytes(repo), before)

    def test_next_is_read_only_without_a_pending_transition(self) -> None:
        for phase in ("active", "done", "halted"):
            with self.subTest(phase=phase):
                repo = self.repository(f"next-{phase}")
                if phase == "active":
                    self.initialize(
                        repo,
                        verify=self.python_verify(
                            "from pathlib import Path; Path('next-ran-verifier').write_text('ran')"
                        ),
                    )
                elif phase == "done":
                    self.initialize(repo)
                    self.assert_returncode(self.complete(repo, "finished", done=True), 0)
                else:
                    self.initialize(repo, max_cycles=1)
                    self.assert_returncode(self.complete(repo, "increment"), 0)
                prompt_path = self.run_dir(repo) / "prompt.md"
                before = (
                    self.state_path(repo).read_bytes(),
                    self.history_path(repo).read_bytes(),
                    prompt_path.read_bytes(),
                )

                result = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(result, 0)
                after = (
                    self.state_path(repo).read_bytes(),
                    self.history_path(repo).read_bytes(),
                    prompt_path.read_bytes(),
                )
                self.assertEqual(after, before)
                self.assertFalse((repo / "next-ran-verifier").exists())

    def test_failed_verification_can_be_fixed_then_completed(self) -> None:
        repo = self.repository("verify-then-fix")
        verify = self.python_verify(
            "from pathlib import Path; raise SystemExit(0 if Path('ready').is_file() else 1)"
        )
        self.initialize(repo, verify=verify)

        self.assert_returncode(self.complete(repo, "first attempt", done=True), 0)
        first = self.read_state(repo)
        self.assertEqual(first["phase"], "active")
        self.assertIs((first["last_verify"] or {})["ok"], False)
        (repo / "ready").write_text("ready", encoding="utf-8")
        self.assert_returncode(self.complete(repo, "fixed", done=True), 0)

        state = self.read_state(repo)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["cycle"], 2)
        self.assertIs((state["last_verify"] or {})["ok"], True)
        self.assertEqual(len(self.read_history(repo)), 2)

    def test_force_restart_keeps_existing_history(self) -> None:
        repo = self.repository("force-restart")
        self.initialize(repo, prompt="old objective")
        self.assert_returncode(self.complete(repo, "first increment"), 0)
        history_before = self.history_path(repo).read_bytes()

        result = self.initialize(repo, prompt="new objective", force=True)

        self.assert_returncode(result, 0)
        state = self.read_state(repo)
        history_after = self.history_path(repo).read_bytes()
        self.assertEqual(state["objective"], "new objective")
        self.assertEqual(state["cycle"], 0)
        self.assertTrue(history_after.startswith(history_before))
        history = self.read_history(repo)
        self.assertEqual(history[0]["evidence"], "first increment")
        self.assertEqual(history[-1]["event"], "restart")

    def test_nonzero_signal_and_timeout_verifiers_leave_consistent_phases(self) -> None:
        cases = (
            (
                "nonzero",
                self.python_verify("raise SystemExit(7)"),
                2,
                None,
                "active",
                7,
            ),
            (
                "signal",
                self.python_verify(
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
                ),
                2,
                None,
                "active",
                None,
            ),
            (
                "timeout",
                self.python_verify("import time; time.sleep(2)"),
                1,
                {"UNTIL_LOOP_VERIFY_TIMEOUT": "0.15"},
                "halted",
                124,
            ),
        )
        for name, verify, max_cycles, env, phase, exit_code in cases:
            with self.subTest(name=name):
                repo = self.repository(f"verifier-{name}")
                self.initialize(repo, verify=verify, max_cycles=max_cycles)
                result = self.complete(repo, "attempt", done=True, env=env, timeout=8)
                self.assert_returncode(result, 0)
                state = self.read_state(repo)
                last_verify = state["last_verify"] or {}
                self.assertEqual(state["phase"], phase)
                self.assertIs(last_verify["ok"], False)
                if exit_code is None:
                    self.assertNotEqual(last_verify["exit"], 0)
                else:
                    self.assertEqual(last_verify["exit"], exit_code)

    def test_nested_non_git_directory_remains_supported(self) -> None:
        nested = self.root / "outer" / "nested" / "work"
        nested.mkdir(parents=True)

        self.initialize(nested)

        self.assertTrue(self.state_path(nested).is_file())

    def test_nested_run_dir_symlink_is_refused_without_external_writes(self) -> None:
        nested = self.root / "outer" / "nested" / "work"
        nested.mkdir(parents=True)
        redirected = self.repository("redirected-run-dir")
        (redirected / "sentinel").write_bytes(b"outside data")
        run_dir = self.run_dir(nested)
        run_dir.symlink_to(redirected, target_is_directory=True)
        before = self.tree_snapshot(redirected)

        result = self.run_cli("init", "--repo", str(nested), "--prompt", "objective")

        self.assert_returncode(result, 2)
        self.assertTrue(run_dir.is_symlink())
        self.assertEqual(self.tree_snapshot(redirected), before)

    def test_metadata_symlinks_are_refused_without_target_writes(self) -> None:
        names = ("state.json", "state.json.tmp", "prompt.md", "history.jsonl", ".lock")
        for name in names:
            with self.subTest(name=name):
                repo = self.repository(f"metadata-{name.replace('.', '_')}")
                self.initialize(repo)
                run_dir = self.run_dir(repo)
                source = run_dir / name
                target = self.root / f"external-{name.replace('.', '_')}"
                if name == "state.json":
                    target.write_bytes(source.read_bytes())
                else:
                    target.write_bytes(b"outside data")
                if source.exists() or source.is_symlink():
                    source.unlink()
                source.symlink_to(target)
                before_target = target.read_bytes()
                before_state = self.state_path(repo).read_bytes()
                before_history = self.history_path(repo).read_bytes()
                if name == "prompt.md":
                    result = self.run_cli(
                        "init", "--repo", str(repo), "--prompt", "replacement", "--force"
                    )
                else:
                    result = self.complete(repo, "evidence")

                self.assert_returncode(result, 2)
                self.assertTrue(source.is_symlink())
                self.assertEqual(target.read_bytes(), before_target)
                self.assertEqual(self.state_path(repo).read_bytes(), before_state)
                self.assertEqual(self.history_path(repo).read_bytes(), before_history)

    def test_hardlinked_metadata_is_refused_before_verification_or_mutation(self) -> None:
        names = (
            "state.json",
            "state.json.tmp",
            "prompt.md",
            "history.jsonl",
            ".lock",
            ".pending.json",
        )
        verifier = self.python_verify(
            "from pathlib import Path; Path('verifier-ran').write_text('ran')"
        )
        for name in names:
            with self.subTest(name=name):
                repo = self.repository(f"hardlink-{name.replace('.', '_')}")
                self.initialize(repo, verify=verifier)
                run_dir = self.run_dir(repo)
                source = run_dir / name
                if not source.exists():
                    if name == ".pending.json":
                        state = self.read_state(repo)
                        source.write_text(
                            json.dumps(
                                {
                                    "state": state,
                                    "event": None,
                                    "history_size": self.history_path(repo).stat().st_size,
                                    "prompt": state["objective"],
                                }
                            ),
                            encoding="utf-8",
                        )
                    else:
                        source.write_bytes(b"metadata marker")
                target = self.root / f"external-hardlink-{name.replace('.', '_')}"
                os.link(source, target)
                self.assertGreater(source.stat().st_nlink, 1)
                before_target = target.read_bytes()
                before = self.tree_snapshot(run_dir)

                result = self.complete(repo, "evidence", done=True)

                self.assert_returncode(result, 2)
                self.assertEqual(target.read_bytes(), before_target)
                self.assertEqual(self.tree_snapshot(run_dir), before)
                self.assertFalse((repo / "verifier-ran").exists())

    def test_unsafe_git_exclude_targets_are_skipped(self) -> None:
        def exclude_path(repo: Path) -> Path:
            result = subprocess.run(
                ["git", "rev-parse", "--git-path", "info/exclude"],
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            path = Path(result.stdout.rstrip("\n"))
            return path if path.is_absolute() else repo / path

        for kind in ("symlink", "directory", "hardlink", "directory-symlink"):
            with self.subTest(kind=kind):
                repo = self.repository(f"unsafe-exclude-{kind}")
                initialized = subprocess.run(
                    ["git", "init", "-q", str(repo)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
                path = exclude_path(repo)
                path.unlink(missing_ok=True)
                external = self.root / f"external-exclude-{kind}"
                before: bytes
                if kind == "symlink":
                    external.write_bytes(b"outside exclude\n")
                    path.symlink_to(external)
                    before = external.read_bytes()
                elif kind == "directory":
                    path.mkdir()
                    before = b""
                elif kind == "hardlink":
                    external.write_bytes(b"outside exclude\n")
                    os.link(external, path)
                    self.assertGreater(path.stat().st_nlink, 1)
                    before = external.read_bytes()
                else:
                    external.mkdir()
                    (external / "exclude").write_bytes(b"outside exclude\n")
                    path.parent.rmdir()
                    path.parent.symlink_to(external, target_is_directory=True)
                    before = (external / "exclude").read_bytes()

                result = self.initialize(repo, prompt="objective")

                self.assert_returncode(result, 0)
                if kind == "symlink":
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(external.read_bytes(), before)
                elif kind == "directory":
                    self.assertTrue(path.is_dir())
                elif kind == "hardlink":
                    self.assertGreater(path.stat().st_nlink, 1)
                    self.assertEqual(external.read_bytes(), before)
                else:
                    self.assertTrue(path.parent.is_symlink())
                    self.assertEqual((external / "exclude").read_bytes(), before)

    def test_verifier_output_is_bounded_in_state_and_packet(self) -> None:
        repo = self.repository("bounded-output")
        verify = self.python_verify(
            "import sys; sys.stdout.write('o' * 150000); "
            "sys.stderr.write('e' * 150000); raise SystemExit(9)"
        )
        self.initialize(repo, verify=verify)

        result = self.complete(repo, "attempt", done=True)

        self.assert_returncode(result, 0)
        state = self.read_state(repo)
        tail = (state["last_verify"] or {})["tail"]
        self.assertEqual(state["phase"], "active")
        self.assertLessEqual(len(tail.encode("utf-8")), 65_536)
        self.assertLess(len(result.stdout), 70_000)

    def test_legacy_tail_render_is_bounded_and_pending_tail_is_rejected(self) -> None:
        verify = self.python_verify("raise SystemExit(1)")
        for location in ("state", "pending"):
            with self.subTest(location=location):
                repo = self.repository(f"oversized-tail-{location}")
                self.initialize(repo, verify=verify)
                self.assert_returncode(self.complete(repo, "attempt", done=True), 0)
                target_state = json.loads(json.dumps(self.read_state(repo)))
                target_state["last_verify"]["tail"] = "x" * 5_000_000
                pending_path = self.run_dir(repo) / ".pending.json"
                if location == "state":
                    self.state_path(repo).write_text(
                        json.dumps(target_state), encoding="utf-8"
                    )
                else:
                    payload = {
                        "state": target_state,
                        "event": self.read_history(repo)[-1],
                        "history_size": 0,
                        "prompt": None,
                    }
                    pending_path.write_text(json.dumps(payload), encoding="utf-8")
                before_state_history = self.state_and_history_bytes(repo)
                before_pending = (
                    pending_path.read_bytes() if pending_path.exists() else None
                )

                result = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(result, 0 if location == "state" else 2)
                if location == "state":
                    self.assertLess(len(result.stdout), 70_000)
                    self.assertIn(b"truncated", result.stdout)
                else:
                    self.assertEqual(result.stdout, b"")
                self.assertEqual(self.state_and_history_bytes(repo), before_state_history)
                after_pending = pending_path.read_bytes() if pending_path.exists() else None
                self.assertEqual(after_pending, before_pending)

    def test_original_v1_runs_resume_and_force_restart_without_data_loss(self) -> None:
        for name in ("whitespace", "noisy", "blank-options", "oversized-evidence"):
            with self.subTest(legacy_run=name):
                fixture = json.loads((SKILL_ROOT / "tests" / "fixtures" /
                                      f"legacy-{name}-v1.json").read_text())
                self.assertEqual(fixture["generated_by_commit"],
                                 "7fb7057056552438fa39ccf11b70fa7c63f80077")
                repo = self.repository("upgrade-" + name)
                self.initialize(repo)
                state = fixture["state"]
                state["repo_root"] = str(repo.resolve())
                self.state_path(repo).write_text(json.dumps(state))
                self.history_path(repo).write_text("".join(json.dumps(e) + "\n" for e in fixture["history"]))
                (self.run_dir(repo) / "prompt.md").write_text(state["objective"] + "\n")
                before = self.tree_snapshot(self.run_dir(repo))
                history_before = self.history_path(repo).read_bytes()

                resumed = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(resumed, 0)
                self.assertEqual(self.tree_snapshot(self.run_dir(repo)), before)
                self.assertLess(len(resumed.stdout), 70_000)
                if name == "whitespace":
                    self.assertIn(b"legacy evidence was blank", resumed.stdout)
                elif name == "noisy":
                    self.assertIn(b"truncated", resumed.stdout)
                elif name == "blank-options":
                    self.assertIn(b"legacy predicate was blank", resumed.stdout)
                    self.assert_returncode(self.complete(repo, "new evidence", done=True), 2)
                    self.assertEqual(self.tree_snapshot(self.run_dir(repo)), before)
                else:
                    self.assertIn(b"truncated", resumed.stdout)
                    self.assertLess(len(resumed.stdout), 10_000)

                self.initialize(repo, prompt="new run after upgrade", force=True)

                self.assertEqual(self.read_state(repo)["cycle"], 0)
                self.assertEqual(self.read_state(repo)["objective"], "new run after upgrade")
                self.assertTrue(self.history_path(repo).read_bytes().startswith(history_before))
                self.assertEqual(self.read_history(repo)[-1]["event"], "restart")

    def test_new_evidence_limit_is_enforced_before_mutation(self) -> None:
        repo = self.repository("evidence-limit")
        self.initialize(repo)
        before = self.tree_snapshot(self.run_dir(repo))
        rejected = self.complete(repo, "x" * 4097, done=True)
        self.assert_returncode(rejected, 64)
        self.assertEqual(self.tree_snapshot(self.run_dir(repo)), before)
        accepted = self.complete(repo, "x" * 4096, done=True)
        self.assert_returncode(accepted, 0)
        self.assertEqual(self.read_state(repo)["last_evidence"], "x" * 4096)
        self.assertLess(len(accepted.stdout), 10_000)

    def test_long_frozen_text_is_preserved_with_bounded_packet_previews(self) -> None:
        repo = self.repository("long-objective")
        objective, predicate = "objective " * 10_000, "predicate " * 10_000
        packet = self.initialize(repo, prompt=objective, done_when=predicate)
        self.assertLess(len(packet.stdout), 20_000)
        self.assertIn(b"truncated", packet.stdout)
        self.assertIn(b"read .until-loop/prompt.md and .until-loop/state.json", packet.stdout)
        self.assertEqual(self.read_state(repo)["objective"], objective)
        self.assertEqual(self.read_state(repo)["done_when"], predicate)
        self.assertEqual((self.run_dir(repo) / "prompt.md").read_text(), objective + "\n")
        before = self.tree_snapshot(self.run_dir(repo))
        resumed = self.run_cli("next", "--repo", str(repo))
        self.assert_returncode(resumed, 0)
        self.assertLess(len(resumed.stdout), 20_000)
        self.assertEqual(self.tree_snapshot(self.run_dir(repo)), before)

    def test_pending_replay_repairs_partial_and_complete_history_once(self) -> None:
        for history_form in ("partial", "complete"):
            with self.subTest(history_form=history_form):
                repo = self.repository(f"pending-{history_form}")
                self.initialize(repo)
                before_state = self.read_state(repo)
                target_state = json.loads(json.dumps(before_state))
                target_state["cycle"] = 1
                target_state["last_evidence"] = "replayed"
                event = {
                    "cycle": 1,
                    "evidence": "replayed",
                    "done_claim": False,
                    "verify_ok": None,
                }
                event_bytes = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
                history_path = self.history_path(repo)
                history_size = history_path.stat().st_size
                existing = history_path.read_bytes()
                suffix = (
                    event_bytes[: max(1, len(event_bytes) // 2)]
                    if history_form == "partial"
                    else event_bytes
                )
                history_path.write_bytes(existing + suffix)
                pending_path = self.run_dir(repo) / ".pending.json"
                pending_path.write_text(
                    json.dumps(
                        {
                            "state": target_state,
                            "event": event,
                            "history_size": history_size,
                            "prompt": None,
                        }
                    ),
                    encoding="utf-8",
                )

                result = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(result, 0)
                self.assertFalse(pending_path.exists())
                state = self.read_state(repo)
                self.assertEqual(state["cycle"], 1)
                self.assertEqual(state["last_evidence"], "replayed")
                self.assertEqual(history_path.read_bytes(), existing + event_bytes)
                self.assertEqual(self.read_history(repo), [event])
                after_replay = self.state_and_history_bytes(repo)

                second = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(second, 0)
                self.assertEqual(self.state_and_history_bytes(repo), after_replay)

    def test_pending_recovery_refuses_missing_mismatched_or_invalid_prompt_before_body(self) -> None:
        verifier = self.python_verify(
            "from pathlib import Path; Path('verifier-ran').write_text('ran')"
        )
        for prompt_form in ("missing", "mismatched", "invalid-utf8"):
            with self.subTest(prompt_form=prompt_form):
                repo = self.repository(f"pending-prompt-{prompt_form}")
                self.initialize(repo, verify=verifier)
                state = self.read_state(repo)
                state.update(
                    cycle=1,
                    last_evidence="replayed",
                    last_verify={"ok": True, "exit": 0, "tail": ""},
                )
                event = {
                    "cycle": 1,
                    "evidence": "replayed",
                    "done_claim": False,
                    "verify_ok": True,
                }
                pending = self.run_dir(repo) / ".pending.json"
                pending.write_text(
                    json.dumps(
                        {
                            "state": state,
                            "event": event,
                            "history_size": self.history_path(repo).stat().st_size,
                            "prompt": None,
                        }
                    ),
                    encoding="utf-8",
                )
                prompt = self.run_dir(repo) / "prompt.md"
                if prompt_form == "missing":
                    prompt.unlink()
                elif prompt_form == "mismatched":
                    prompt.write_text(state["objective"], encoding="utf-8")
                else:
                    prompt.write_bytes(b"\xff\n")

                result = self.complete(repo, "should-not-run", done=True)

                self.assert_returncode(result, 2)
                self.assertFalse(pending.exists())
                recovered = self.read_state(repo)
                self.assertEqual(recovered["cycle"], 1)
                self.assertEqual(recovered["last_evidence"], "replayed")
                self.assertEqual(self.read_history(repo), [event])
                self.assertFalse((repo / "verifier-ran").exists())
                if prompt_form == "missing":
                    self.assertFalse(prompt.exists())
                elif prompt_form == "mismatched":
                    self.assertEqual(prompt.read_text(encoding="utf-8"), state["objective"])
                else:
                    self.assertEqual(prompt.read_bytes(), b"\xff\n")

    def test_settled_prompt_check_preserves_literal_crlf_and_unicode(self) -> None:
        repo = self.repository("literal-prompt-bytes")
        objective = "first line\r\nsecond é line\rthird line"
        self.initialize(repo, prompt=objective)
        prompt = self.run_dir(repo) / "prompt.md"

        resumed = self.run_cli("next", "--repo", str(repo))

        self.assert_returncode(resumed, 0)
        self.assertEqual(prompt.read_bytes(), (objective + "\n").encode("utf-8"))

    def test_inconsistent_pending_event_is_refused_without_mutation(self) -> None:
        for mismatch in ("cycle", "evidence", "done_claim", "verify_ok", "prompt", "restart-prompt"):
            with self.subTest(mismatch=mismatch):
                repo = self.repository("pending-" + mismatch)
                self.initialize(repo)
                state = self.read_state(repo)
                state.update(cycle=1, last_evidence="prepared")
                event = {"cycle": 1, "evidence": "prepared", "done_claim": False, "verify_ok": None}
                tx = {"state": state, "event": event, "history_size": 0, "prompt": None}
                if mismatch in ("cycle", "evidence", "done_claim", "verify_ok"):
                    event[mismatch] = {"cycle": 9, "evidence": "different", "done_claim": True,
                                       "verify_ok": True}[mismatch]
                elif mismatch == "prompt":
                    tx["prompt"] = "unrelated prompt"
                else:
                    state.update(cycle=0, last_evidence=None)
                    tx.update(event=None, prompt="unrelated prompt")
                pending = self.run_dir(repo) / ".pending.json"
                pending.write_text(json.dumps(tx), encoding="utf-8")
                before = self.tree_snapshot(self.run_dir(repo))

                result = self.run_cli("next", "--repo", str(repo))

                self.assert_returncode(result, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(self.tree_snapshot(self.run_dir(repo)), before)

    def test_next_recovers_one_interrupted_transition_then_becomes_read_only(self) -> None:
        runtime = self.load_runtime_module()
        repo = self.repository("fault-injected-transition")
        original_save_state = runtime.save_state

        with mock.patch.object(runtime, "print_packet"):
            self.assertEqual(
                runtime.main(["init", "--repo", str(repo), "--prompt", "objective"]),
                0,
            )

        calls = 0

        def fail_save_once(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected save failure")
            original_save_state(*args, **kwargs)

        with (
            mock.patch.object(runtime, "print_packet"),
            mock.patch.object(runtime, "save_state", side_effect=fail_save_once),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                runtime.main(
                    ["complete", "--repo", str(repo), "--evidence", "interrupted"]
                ),
                2,
            )

        with mock.patch.object(runtime, "print_packet"):
            self.assertEqual(runtime.main(["next", "--repo", str(repo)]), 0)
        state = self.read_state(repo)
        history = self.read_history(repo)
        self.assertEqual(state["cycle"], 1)
        self.assertEqual(state["last_evidence"], "interrupted")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["cycle"], 1)
        after_recovery = self.state_and_history_bytes(repo)

        with mock.patch.object(runtime, "print_packet"):
            self.assertEqual(runtime.main(["next", "--repo", str(repo)]), 0)
        self.assertEqual(self.state_and_history_bytes(repo), after_recovery)


if __name__ == "__main__":
    unittest.main()
