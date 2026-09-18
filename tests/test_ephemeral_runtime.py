from __future__ import annotations

import concurrent.futures
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import until_loop_ephemeral as innerloop
from until_loop_ephemeral import StateError, done, next_packet, read_state, start


SCRIPT = (SCRIPT_DIR / "until_loop_ephemeral.py").resolve()
HUGE_INTEGER = b"9" * 5000
PACKET_FIELDS = {
    "status",
    "state_file",
    "workspace",
    "work",
    "conditions",
    "progress",
    "last_report",
    "instruction",
    "done_argv",
    "report_schema",
}


class InnerloopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def contract(self, **overrides: object) -> dict[str, object]:
        result: dict[str, object] = {
            "workspace": str(self.workspace),
            "work": "Inspect the scoped candidate and report actual observations.",
            "exit_condition": "The scoped goal is established by current evidence.",
            "repeat_condition": "Continue while useful authorized work remains.",
        }
        result.update(overrides)
        return result

    def report(
        self,
        classification: str = "trivial",
        exit_assessment: str = "unsatisfied",
        continuation_assessment: str = "allowed",
        evidence: str = "Observed the scoped result and recorded the remaining gap.",
        **overrides: object,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "classification": classification,
            "exit_assessment": exit_assessment,
            "continuation_assessment": continuation_assessment,
            "evidence": evidence,
        }
        result.update(overrides)
        return result

    def state_path(self, packet: dict[str, object]) -> Path:
        state_file = packet["state_file"]
        self.assertIsInstance(state_file, str)
        path = Path(state_file)
        self.assertTrue(path.is_absolute())
        return path

    def action_id(self, packet: dict[str, object]) -> str:
        argv = packet["done_argv"]
        self.assertIsInstance(argv, list)
        self.assertEqual(len(argv), 6)
        self.assertTrue(all(isinstance(item, str) for item in argv))
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertEqual(Path(argv[1]), SCRIPT)
        self.assertEqual(argv[2:4], ["done", "--state"])
        self.assertEqual(Path(argv[4]), self.state_path(packet))
        self.assertTrue(argv[5].startswith("--action="))
        action_id = argv[5][len("--action=") :]
        self.assertTrue(action_id)
        return action_id

    def assert_packet(
        self,
        packet: dict[str, object],
        *,
        status: str,
        contract: dict[str, object],
        action_number: int | None = None,
        trivial_streak: int | None = None,
        last_report: dict[str, object] | None = None,
    ) -> None:
        self.assertEqual(set(packet), PACKET_FIELDS)
        self.assertEqual(packet["status"], status)
        self.state_path(packet)
        self.assertEqual(packet["workspace"], contract["workspace"])
        self.assertEqual(packet["work"], contract["work"])
        self.assertEqual(
            packet["conditions"],
            {"exit": contract["exit_condition"], "repeat": contract["repeat_condition"]},
        )
        progress = packet["progress"]
        self.assertIsInstance(progress, dict)
        self.assertEqual(
            set(progress),
            {"action_number", "trivial_streak", "required_trivial_reviews"},
        )
        self.assertEqual(
            progress["required_trivial_reviews"],
            contract.get("required_trivial_reviews", 0),
        )
        if action_number is not None:
            self.assertEqual(progress["action_number"], action_number)
        if trivial_streak is not None:
            self.assertEqual(progress["trivial_streak"], trivial_streak)
        self.assertEqual(packet["last_report"], last_report)
        self.assertIsInstance(packet["instruction"], str)
        self.assertTrue(packet["instruction"].strip())
        if status == "active":
            self.assertIsInstance(packet["report_schema"], dict)
            self.action_id(packet)
        else:
            self.assertIn(status, {"complete", "stopped"})
            self.assertIsNone(packet["done_argv"])
            self.assertIsNone(packet["report_schema"])

    def assert_execution_before_done_contract(self, packet: dict[str, object]) -> None:
        instruction = packet["instruction"]
        self.assertIsInstance(instruction, str)
        work = packet["work"]
        self.assertIsInstance(work, str)
        execution_condition = f"Execution condition (Work): {work!r}."
        self.assertIn(execution_condition, instruction)
        self.assertIn("exactly one complete assigned iteration", instruction.casefold())
        self.assertLess(
            instruction.index(execution_condition),
            instruction.index("done_argv JSON array"),
        )

        schema = packet["report_schema"]
        self.assertIsInstance(schema, dict)
        properties = schema["properties"]
        self.assertIsInstance(properties, dict)
        classification = properties["classification"]
        self.assertIsInstance(classification, dict)
        description = classification["description"]
        self.assertIsInstance(description, str)
        self.assertEqual(
            set(classification["enum"]), {"trivial", "non-trivial", "unresolved"}
        )
        for value in classification["enum"]:
            with self.subTest(value=value):
                self.assertIn(value, description)

    def run_cli(
        self,
        argv: list[str],
        payload: object | None = None,
        *,
        expected_returncode: int = 0,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            argv,
            input="" if payload is None else json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(SKILL_ROOT),
            env=environment,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected_returncode,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertTrue(result.stdout.strip(), msg=f"stderr={result.stderr!r}")
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, dict)
        return parsed

    def run_raw_cli(
        self,
        argv: list[str],
        payload: bytes = b"",
        *,
        expected_returncode: int,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(SKILL_ROOT),
            env=environment,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected_returncode,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertEqual(result.stderr, b"")
        self.assertTrue(result.stdout)
        parsed = json.loads(result.stdout.decode("utf-8"))
        self.assertIsInstance(parsed, dict)
        return parsed

    def raw_contract_with_huge_integer(self) -> bytes:
        return (
            b'{"workspace":'
            + json.dumps(str(self.workspace)).encode("utf-8")
            + b',"work":"x","exit_condition":"x","repeat_condition":"x",'
            + b'"required_trivial_reviews":'
            + HUGE_INTEGER
            + b"}"
        )

    def raw_report_with_huge_integer(self) -> bytes:
        return (
            b'{"classification":'
            + HUGE_INTEGER
            + b',"exit_assessment":"unsatisfied","continuation_assessment":"allowed",'
            + b'"evidence":"current observation"}'
        )

    def assert_unchanged_error(self, packet: dict[str, object]) -> None:
        self.assertEqual(packet["status"], "error")
        self.assertEqual(packet["state_change"], "unchanged")

    def cli_start(self, contract: dict[str, object], directory: Path | None = None) -> dict[str, object]:
        argv = [sys.executable, str(SCRIPT), "start"]
        if directory is not None:
            argv.extend(["--directory", str(directory)])
        return self.run_cli(argv, contract)

    def test_start_and_next_packet_preserve_shape_state_and_private_file(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        packet = start(contract, directory=self.directory)
        self.assert_packet(
            packet,
            status="active",
            contract=contract,
            action_number=1,
            trivial_streak=0,
        )
        path = self.state_path(packet)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        before = path.read_bytes()
        state = read_state(path)
        self.assertIsInstance(state, dict)
        self.assertEqual(next_packet(path), packet)
        self.assertEqual(path.read_bytes(), before)

    def test_initial_and_successor_packets_require_execution_before_done(self) -> None:
        requests = (
            (0, "Read the requested value and report it without editing files."),
            (2,
                "Review current artifacts and recent history, plan worthwhile changes, "
                "implement authorized fixes, and run checks."
            ),
        )
        for gate, work in requests:
            with self.subTest(required_trivial_reviews=gate):
                contract = self.contract(work=work, required_trivial_reviews=gate)
                initial = start(contract, directory=self.directory)
                successor = done(
                    self.state_path(initial),
                    self.action_id(initial),
                    self.report("non-trivial", "unsatisfied", "allowed", "A material observation needs rechecking."),
                )
                for packet in (initial, successor):
                    with self.subTest(action_number=packet["progress"]["action_number"]):
                        self.assertEqual(packet["work"], work)
                        self.assertEqual(packet["progress"]["required_trivial_reviews"], gate)
                        self.assert_execution_before_done_contract(packet)
                        self.assertIn("For an Improve request,", packet["instruction"])

    def test_cli_callbacks_run_in_new_processes_and_next_is_byte_stable(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        initial = self.cli_start(contract)
        self.assert_packet(
            initial,
            status="active",
            contract=contract,
            action_number=1,
            trivial_streak=0,
        )
        path = self.state_path(initial)
        before = path.read_bytes()
        next_from_new_process = self.run_cli(
            [sys.executable, str(SCRIPT), "next", "--state", str(path)]
        )
        self.assertEqual(next_from_new_process, initial)
        self.assertEqual(path.read_bytes(), before)

        first_report = self.report("trivial", "satisfied", "allowed", "First distinct clean review.")
        second = self.run_cli(initial["done_argv"], first_report)  # type: ignore[arg-type]
        self.assert_packet(
            second,
            status="active",
            contract=contract,
            action_number=2,
            trivial_streak=1,
            last_report=first_report,
        )
        second_report = self.report("trivial", "satisfied", "allowed", "Second distinct clean review.")
        terminal = self.run_cli(second["done_argv"], second_report)  # type: ignore[arg-type]
        self.assert_packet(
            terminal,
            status="complete",
            contract=contract,
            last_report=second_report,
        )
        self.assertFalse(path.exists())

    def test_leading_dash_action_token_uses_emitted_equals_argv(self) -> None:
        contract = self.contract(required_trivial_reviews=0)
        with mock.patch.object(innerloop.secrets, "token_urlsafe", return_value="-leading-token"):
            packet = start(contract, directory=self.directory)
        path = self.state_path(packet)
        self.assertEqual(self.action_id(packet), "-leading-token:1")

        report = self.report(
            "trivial",
            "satisfied",
            "allowed",
            "The leading-dash action token completed through the emitted argv.",
        )
        terminal = self.run_cli(packet["done_argv"], report)  # type: ignore[arg-type]
        self.assert_packet(terminal, status="complete", contract=contract, last_report=report)
        self.assertFalse(path.exists())

    def test_terminal_surrogate_report_emits_utf8_json_and_cleans_up(self) -> None:
        contract = self.contract(required_trivial_reviews=0)
        initial = self.cli_start(contract)
        path = self.state_path(initial)
        surrogate_report = self.report("trivial", "satisfied", "allowed", "\ud800")
        terminal = self.run_raw_cli(
            initial["done_argv"],  # type: ignore[arg-type]
            b'{"classification":"trivial","exit_assessment":"satisfied",'
            b'"continuation_assessment":"allowed","evidence":"\\ud800"}',
            expected_returncode=0,
        )
        self.assert_packet(
            terminal,
            status="complete",
            contract=contract,
            last_report=surrogate_report,
        )
        last_report = terminal["last_report"]
        self.assertIsInstance(last_report, dict)
        self.assertEqual(last_report["evidence"], "\ud800")
        self.assertFalse(path.exists())

    def test_cli_rejects_huge_numeric_contract_without_creating_state(self) -> None:
        before = set(self.directory.iterdir())
        error_packet = self.run_raw_cli(
            [sys.executable, str(SCRIPT), "start", "--directory", str(self.directory)],
            self.raw_contract_with_huge_integer(),
            expected_returncode=2,
        )
        self.assert_unchanged_error(error_packet)
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_cli_rejects_huge_numeric_callback_without_advancing_state(self) -> None:
        packet = self.cli_start(self.contract())
        path = self.state_path(packet)
        before = path.read_bytes()
        error_packet = self.run_raw_cli(
            packet["done_argv"],  # type: ignore[arg-type]
            self.raw_report_with_huge_integer(),
            expected_returncode=2,
        )
        self.assert_unchanged_error(error_packet)
        self.assertEqual(path.read_bytes(), before)

    def test_cli_rejects_huge_numeric_persisted_state_without_rewriting_it(self) -> None:
        packet = start(self.contract(), directory=self.directory)
        path = self.state_path(packet)
        original = path.read_bytes()
        corrupted = original.replace(
            b'"action_number":1',
            b'"action_number":' + HUGE_INTEGER,
            1,
        )
        self.assertNotEqual(corrupted, original)
        self.assertLess(len(corrupted), innerloop.MAX_STATE_BYTES)
        path.write_bytes(corrupted)
        error_packet = self.run_raw_cli(
            [sys.executable, str(SCRIPT), "next", "--state", str(path)],
            expected_returncode=2,
        )
        self.assert_unchanged_error(error_packet)
        self.assertEqual(path.read_bytes(), corrupted)

    def test_two_clean_gate_requires_the_second_callback(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        initial = start(contract, directory=self.directory)
        path = self.state_path(initial)
        first_report = self.report("trivial", "satisfied", "allowed", "First review is clean.")
        second = done(path, self.action_id(initial), first_report)
        self.assert_packet(
            second,
            status="active",
            contract=contract,
            action_number=2,
            trivial_streak=1,
            last_report=first_report,
        )

        second_report = self.report("trivial", "satisfied", "blocked", "Second review is clean.")
        terminal = done(path, self.action_id(second), second_report)
        self.assert_packet(
            terminal,
            status="complete",
            contract=contract,
            trivial_streak=2,
            last_report=second_report,
        )
        self.assertFalse(path.exists())

        blocked_before_gate = start(contract, directory=self.directory)
        blocked_report = self.report("trivial", "satisfied", "blocked", "The first review is clean.")
        stopped = done(
            self.state_path(blocked_before_gate),
            self.action_id(blocked_before_gate),
            blocked_report,
        )
        self.assert_packet(stopped, status="stopped", contract=contract, last_report=blocked_report)

    def test_gate_two_decision_precedence_at_streak_zero_and_one(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        cases = (
            (0, self.report("trivial", "satisfied", "blocked", "Gate is not met."), "stopped"),
            (0, self.report("trivial", "satisfied", "cancelled", "Cancelled before completion."), "stopped"),
            (0, self.report("unresolved", "satisfied", "allowed", "The evidence gap remains."), "error"),
            (1, self.report("trivial", "satisfied", "blocked", "Gate is now met."), "complete"),
            (1, self.report("trivial", "satisfied", "cancelled", "Cancelled after a clean review."), "stopped"),
            (1, self.report("unresolved", "satisfied", "cancelled", "The evidence gap remains."), "error"),
        )
        for initial_streak, report, expected in cases:
            with self.subTest(initial_streak=initial_streak, report=report, expected=expected):
                packet = start(contract, directory=self.directory)
                path = self.state_path(packet)
                if initial_streak:
                    packet = done(
                        path,
                        self.action_id(packet),
                        self.report("trivial", "unsatisfied", "allowed", "First clean review."),
                    )
                before = path.read_bytes()
                if expected == "error":
                    with self.assertRaises(StateError):
                        done(path, self.action_id(packet), report)
                    self.assertEqual(path.read_bytes(), before)
                else:
                    terminal = done(path, self.action_id(packet), report)
                    self.assert_packet(terminal, status=expected, contract=contract, last_report=report)
                    self.assertFalse(path.exists())

    def test_non_trivial_and_unresolved_reports_reset_the_streak(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        packet = start(contract, directory=self.directory)
        path = self.state_path(packet)
        reports = (
            (self.report("trivial", "unsatisfied", "allowed", "First clean review."), 1),
            (self.report("non-trivial", "unsatisfied", "allowed", "Found a material issue."), 0),
            (self.report("trivial", "unknown", "allowed", "A separate clean review."), 1),
            (self.report("unresolved", "unknown", "allowed", "Evidence is still incomplete."), 0),
        )
        for action_number, (report, expected_streak) in enumerate(reports, start=1):
            with self.subTest(action_number=action_number, report=report):
                packet = done(path, self.action_id(packet), report)
                self.assert_packet(
                    packet,
                    status="active",
                    contract=contract,
                    action_number=action_number + 1,
                    trivial_streak=expected_streak,
                    last_report=report,
                )

    def test_full_zero_gate_decision_matrix(self) -> None:
        contract = self.contract(required_trivial_reviews=0)
        for classification in ("trivial", "non-trivial", "unresolved"):
            for exit_assessment in ("satisfied", "unsatisfied", "unknown"):
                for continuation_assessment in ("allowed", "blocked", "cancelled"):
                    with self.subTest(
                        classification=classification,
                        exit_assessment=exit_assessment,
                        continuation_assessment=continuation_assessment,
                    ):
                        packet = start(contract, directory=self.directory)
                        path = self.state_path(packet)
                        report = self.report(
                            classification,
                            exit_assessment,
                            continuation_assessment,
                            "Observed this decision-matrix case.",
                        )
                        before = path.read_bytes()
                        if classification == "unresolved" and exit_assessment == "satisfied":
                            expected = "error"
                        elif continuation_assessment == "cancelled":
                            expected = "stopped"
                        elif exit_assessment == "satisfied":
                            expected = "complete"
                        elif continuation_assessment == "blocked":
                            expected = "stopped"
                        else:
                            expected = "active"

                        if expected == "error":
                            with self.assertRaises(StateError):
                                done(path, self.action_id(packet), report)
                            self.assertEqual(path.read_bytes(), before)
                            continue

                        result = done(path, self.action_id(packet), report)
                        if expected == "active":
                            self.assert_packet(
                                result,
                                status="active",
                                contract=contract,
                                action_number=2,
                                trivial_streak=1 if classification == "trivial" else 0,
                                last_report=report,
                            )
                            self.assertTrue(path.exists())
                        else:
                            self.assert_packet(
                                result,
                                status=expected,
                                contract=contract,
                                last_report=report,
                            )
                            self.assertFalse(path.exists())

    def test_stale_cross_run_and_invalid_callbacks_leave_bytes_unchanged(self) -> None:
        contract = self.contract(required_trivial_reviews=2)
        packet = start(contract, directory=self.directory)
        path = self.state_path(packet)
        stale_action = self.action_id(packet)
        current = done(
            path,
            stale_action,
            self.report("trivial", "unsatisfied", "allowed", "Advance to a new callback."),
        )
        current_action = self.action_id(current)
        before = path.read_bytes()
        valid_report = self.report("trivial", "unsatisfied", "allowed", "Use the current callback.")

        for stale in (stale_action, "not-a-current-action", ""):
            with self.subTest(stale=stale):
                with self.assertRaises(StateError):
                    done(path, stale, valid_report)
                self.assertEqual(path.read_bytes(), before)

        for bad_action in (None, 1, True, []):
            with self.subTest(bad_action=bad_action):
                with self.assertRaises(StateError):
                    done(path, bad_action, valid_report)  # type: ignore[arg-type]
                self.assertEqual(path.read_bytes(), before)

        other = start(self.contract(required_trivial_reviews=2), directory=self.directory)
        other_path = self.state_path(other)
        other_before = other_path.read_bytes()
        with self.assertRaises(StateError):
            done(other_path, current_action, valid_report)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(other_path.read_bytes(), other_before)

        invalid_reports = (
            self.report(decision="complete"),
            self.report(next_action="caller-selected successor"),
            self.report(successor="caller-selected successor"),
            self.report(count=99),
            self.report(classification="unknown"),
            self.report(classification=[]),
            self.report(exit_assessment=True),
            self.report(continuation_assessment=[]),
            self.report(evidence=""),
            self.report(evidence=1),
        )
        for invalid_report in invalid_reports:
            with self.subTest(invalid_report=invalid_report):
                with self.assertRaises(StateError):
                    done(path, current_action, invalid_report)
                self.assertEqual(path.read_bytes(), before)

        error_packet = self.run_cli(
            current["done_argv"],  # type: ignore[arg-type]
            self.report(successor="caller-selected successor"),
            expected_returncode=2,
        )
        self.assertEqual(error_packet["status"], "error")
        self.assertEqual(error_packet["state_change"], "unchanged")
        self.assertEqual(path.read_bytes(), before)

    def test_oversized_reports_are_rejected_before_active_or_terminal_writes(self) -> None:
        for exit_assessment in ("unsatisfied", "satisfied"):
            with self.subTest(exit_assessment=exit_assessment):
                packet = start(self.contract(required_trivial_reviews=0), directory=self.directory)
                path = self.state_path(packet)
                before = path.read_bytes()
                oversized = self.report(
                    "trivial",
                    exit_assessment,
                    "allowed",
                    evidence="x" * (256 * 1024),
                )
                with self.assertRaises(StateError):
                    done(path, self.action_id(packet), oversized)
                self.assertEqual(path.read_bytes(), before)

    def test_partial_write_error_reports_unknown_state_without_replaying_work(self) -> None:
        packet = start(self.contract(), directory=self.directory)
        path = self.state_path(packet)
        action = self.action_id(packet)
        report = self.report("trivial", "unsatisfied", "allowed", "A callback was already completed.")

        original_write = innerloop.os.write
        writes = 0

        def short_write_then_fail(fd: int, payload: bytes) -> int:
            nonlocal writes
            if writes == 0:
                writes += 1
                return original_write(fd, payload[:8])
            raise OSError("simulated disk error")

        before = path.read_bytes()
        with mock.patch.object(innerloop.os, "write", side_effect=short_write_then_fail):
            with self.assertRaises(OSError):
                done(path, action, report)
        partial = path.read_bytes()
        self.assertTrue(path.exists())
        self.assertNotEqual(partial, before)
        self.assertGreater(len(partial), 0)
        self.assertLess(len(partial), len(before))
        with self.assertRaises(StateError):
            next_packet(path)

        cli_packet = start(self.contract(), directory=self.directory)
        cli_path = self.state_path(cli_packet)
        cli_before = cli_path.read_bytes()
        standard_input = io.StringIO(json.dumps(report))
        standard_output = io.StringIO()
        writes = 0
        with (
            mock.patch.object(innerloop.os, "write", side_effect=short_write_then_fail),
            mock.patch.object(sys, "stdin", standard_input),
            mock.patch.object(sys, "stdout", standard_output),
        ):
            exit_code = innerloop.main(
                ["done", "--state", str(cli_path), "--action", self.action_id(cli_packet)]
            )
        self.assertEqual(exit_code, 2)
        error_packet = json.loads(standard_output.getvalue())
        self.assertEqual(error_packet["status"], "error")
        self.assertEqual(error_packet["state_change"], "unknown")
        cli_partial = cli_path.read_bytes()
        self.assertNotEqual(cli_partial, cli_before)
        self.assertGreater(len(cli_partial), 0)
        self.assertLess(len(cli_partial), len(cli_before))
        with self.assertRaises(StateError):
            next_packet(cli_path)

    def test_concurrent_distinct_cli_chains_are_isolated(self) -> None:
        def run_chain(
            index: int,
        ) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
            workspace = self.directory / f"workspace-{index}"
            workspace.mkdir()
            contract = self.contract(
                workspace=str(workspace),
                work=f"Inspect chain {index}.",
                required_trivial_reviews=2,
            )
            initial = self.cli_start(contract, self.directory)
            report = self.report("trivial", "unsatisfied", "allowed", f"Chain {index} first review.")
            current = self.run_cli(initial["done_argv"], report)  # type: ignore[arg-type]
            return contract, initial, current, report

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run_chain, (1, 2)))

        paths: set[Path] = set()
        for contract, initial, current, report in results:
            self.assert_packet(
                initial,
                status="active",
                contract=contract,
                action_number=1,
                trivial_streak=0,
            )
            self.assert_packet(
                current,
                status="active",
                contract=contract,
                action_number=2,
                trivial_streak=1,
                last_report=report,
            )
            paths.add(self.state_path(initial))
        self.assertEqual(len(paths), 2)

    def test_special_character_state_paths_use_emitted_argv_without_shell_quoting(self) -> None:
        state_directory = self.directory / "state ; $dollar 'quoted' [brackets]"
        workspace = self.directory / "workspace ; $dollar 'quoted' [brackets]"
        state_directory.mkdir()
        workspace.mkdir()
        contract = self.contract(workspace=str(workspace), required_trivial_reviews=0)
        packet = start(contract, directory=state_directory)
        path = self.state_path(packet)
        self.assertEqual(path.parent, state_directory)
        terminal_report = self.report(
            "non-trivial",
            "satisfied",
            "allowed",
            "Observed special characters in the state path: ; $ ' [ ].",
        )
        terminal = self.run_cli(
            packet["done_argv"],  # type: ignore[arg-type]
            terminal_report,
        )
        self.assert_packet(
            terminal,
            status="complete",
            contract=contract,
            last_report=terminal_report,
        )
        self.assertFalse(path.exists())

    def test_start_rejects_unknown_or_bad_contracts_before_creating_state(self) -> None:
        original_children = set(self.directory.iterdir())
        invalid_contracts = (
            self.contract(decision="complete"),
            self.contract(next_action="caller-selected successor"),
            self.contract(workspace="relative-workspace"),
            self.contract(workspace=str(self.directory / "missing-workspace")),
            self.contract(work=" "),
            self.contract(required_trivial_reviews=True),
            self.contract(required_trivial_reviews=-1),
        )
        for contract in invalid_contracts:
            with self.subTest(contract=contract):
                with self.assertRaises(StateError):
                    start(contract, directory=self.directory)
                self.assertEqual(set(self.directory.iterdir()), original_children)

    def test_missing_and_corrupt_state_files_are_not_recreated(self) -> None:
        missing = start(self.contract(), directory=self.directory)
        missing_path = self.state_path(missing)
        missing_path.unlink()
        for operation in (
            lambda: read_state(missing_path),
            lambda: next_packet(missing_path),
            lambda: done(missing_path, self.action_id(missing), self.report()),
        ):
            with self.assertRaises(StateError):
                operation()
        self.assertFalse(missing_path.exists())

        corrupt = start(self.contract(), directory=self.directory)
        corrupt_path = self.state_path(corrupt)
        corrupt_path.write_bytes(b"{not-json")
        before = corrupt_path.read_bytes()
        for operation in (
            lambda: read_state(corrupt_path),
            lambda: next_packet(corrupt_path),
            lambda: done(corrupt_path, self.action_id(corrupt), self.report()),
        ):
            with self.assertRaises(StateError):
                operation()
            self.assertEqual(corrupt_path.read_bytes(), before)

    def test_symlink_state_path_is_rejected_without_following_it(self) -> None:
        packet = start(self.contract(), directory=self.directory)
        path = self.state_path(packet)
        alias = self.directory / "state-alias.json"
        try:
            alias.symlink_to(path)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        before = path.read_bytes()
        for operation in (
            lambda: read_state(alias),
            lambda: next_packet(alias),
            lambda: done(alias, self.action_id(packet), self.report()),
        ):
            with self.assertRaises(StateError):
                operation()
            self.assertEqual(path.read_bytes(), before)
        alias.unlink()

    def test_hardlinked_state_file_is_rejected_without_mutation(self) -> None:
        packet = start(self.contract(), directory=self.directory)
        path = self.state_path(packet)
        alias = self.directory / "state-hardlink.json"
        try:
            os.link(path, alias)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"hardlinks unavailable: {error}")
        try:
            before = path.read_bytes()
            for operation in (
                lambda: read_state(path),
                lambda: next_packet(path),
                lambda: done(path, self.action_id(packet), self.report()),
            ):
                with self.assertRaises(StateError):
                    operation()
                self.assertEqual(path.read_bytes(), before)
        finally:
            alias.unlink()

    def test_owned_unlink_failure_preserves_the_callback_for_retry(self) -> None:
        contract = self.contract(required_trivial_reviews=0)
        packet = start(contract, directory=self.directory)
        path = self.state_path(packet)
        action = self.action_id(packet)
        report = self.report("trivial", "satisfied", "allowed", "Terminal evidence is available.")
        before = path.read_bytes()
        with mock.patch.object(innerloop.Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertRaises(StateError):
                done(path, action, report)
        self.assertEqual(path.read_bytes(), before)
        terminal = done(path, action, report)
        self.assert_packet(terminal, status="complete", contract=contract, last_report=report)
        self.assertFalse(path.exists())
        with self.assertRaises(StateError):
            done(path, action, report)


if __name__ == "__main__":
    unittest.main()
