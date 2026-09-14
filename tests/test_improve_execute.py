#!/usr/bin/env python3
"""Hermetic regressions for the Improve execution-fixture checker."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_execute as fixture


class ImproveExecutionCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="improve-execute-tests-")
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.workspace = self.root / "workspace"
        fixture.prepare(self.evidence, self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def verbose_message(subject: str, *, classification: str, streak: str) -> str:
        streak_sentence = (
            "This material repair resets the clean-review streak to zero."
            if classification == "material" and streak == "zero"
            else f"The clean-review streak is {streak}."
        )
        return f"""{subject}

Review:
Confirmed the current fixture behavior and classified this as a {classification} change.

Plan:
Apply the bounded change and rerun the visible suite.

Changes:
Made the scoped correction recorded by this commit.

Validation:
Ran the fixture unittest suite and the independent oracle.

Key learnings:
The runtime receipt cannot replace project-level validation.

Remaining work:
{streak_sentence}
"""

    def commit_scoped(
        self,
        subject: str,
        *paths: str,
        verbose: bool = True,
        classification: str = "material",
        streak: str = "zero",
        message: str | None = None,
    ) -> None:
        fixture.git(self.workspace, "add", "--", *paths)
        message = message if message is not None else (
            self.verbose_message(subject, classification=classification, streak=streak)
            if verbose else subject
        )
        # --only preserves the deliberately staged unrelated user draft.
        fixture.git(
            self.workspace,
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "--no-verify",
            "--only",
            "-m",
            message,
            "--",
            *paths,
        )

    def write_synthetic_done_receipts(self) -> None:
        """Supply only the checker's mechanical receipt boundary for this unit test.

        These records are deliberately not a real runtime transition and do
        not prove review convergence; protocol behavior is covered by the v2
        suite.  They let these tests isolate the independent checker.
        """
        run_dir = self.workspace / ".until-loop"
        run_dir.mkdir()
        fixture.write_json(run_dir / "state.json", {"version": 2, "phase": "done"})
        history = [
            {"type": "assessment", "decision": "continue", "assessment": {}},
            {"type": "assessment", "decision": "continue", "assessment": {}},
            {"type": "assessment", "decision": "complete", "assessment": {}},
        ]
        (run_dir / "history.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in history), encoding="utf-8"
        )

    def complete_successfully(self, *, second_verbose_commit: bool = True) -> None:
        (self.workspace / "formatter.py").write_text(
            "def format_name(value: str) -> str:\n    return value.strip() or 'Anonymous'\n",
            encoding="utf-8",
        )
        tests = (self.workspace / "test_formatter.py").read_text(encoding="utf-8")
        tests = tests.replace(
            "\n\nif __name__ == '__main__':",
            "\n\n    def test_blank_value_uses_anonymous_fallback(self):\n"
            "        self.assertEqual(format_name('\\u2003\\t\\u00a0'), 'Anonymous')\n"
            "\n\nif __name__ == '__main__':",
        )
        (self.workspace / "test_formatter.py").write_text(tests, encoding="utf-8")
        self.commit_scoped(
            "fix(formatter): honor documented blank fallback",
            "formatter.py", "test_formatter.py",
            classification="material", streak="zero",
        )
        if second_verbose_commit:
            readme = (self.workspace / "README.md").read_text(encoding="utf-8")
            (self.workspace / "README.md").write_text(
                readme + "\nNonblank names retain their original spelling.\n", encoding="utf-8"
            )
            self.commit_scoped(
                "docs(formatter): clarify nonblank name behavior",
                "README.md",
                classification="trivial", streak="one",
            )
        self.write_synthetic_done_receipts()

    def checked_report(self, *, expect_pass: bool) -> dict[str, object]:
        try:
            path = fixture.check(self.evidence, self.workspace)
        except fixture.FixtureError:
            if expect_pass:
                raise
            reports = sorted((self.evidence / "reports").glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
            self.assertTrue(reports, "failed checks must retain a report")
            return json.loads(reports[-1].read_text(encoding="utf-8"))
        if not expect_pass:
            self.fail(f"checker unexpectedly passed: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_checker_accepts_all_verbose_commits_and_preserves_unrelated_index(self) -> None:
        self.complete_successfully()

        report = self.checked_report(expect_pass=True)

        self.assertTrue(report["passed"])
        suite = report["fixture_tests"]
        self.assertEqual(suite["returncode"], 0)
        self.assertGreaterEqual(suite["tests_ran"], 1)
        self.assertTrue(suite["result_complete"])
        self.assertEqual(suite["result_status"], "complete")
        self.assertTrue(suite["successful"])
        self.assertEqual(suite["failures"], 0)
        self.assertEqual(suite["errors"], 0)
        self.assertEqual(suite["skipped_tests"], 0)
        self.assertGreaterEqual(suite["executed_tests"], 1)
        self.assertEqual(suite["cwd"], str(self.workspace.resolve()))
        self.assertEqual(
            suite["command"],
            [sys.executable, str(fixture.UNITTEST_RESULT_LAUNCHER), "--result-fd"],
        )
        self.assertTrue(all(record["verbose_sections"] for record in report["new_commits"]))
        self.assertTrue(all(record["classification_mentioned"] for record in report["new_commits"]))
        self.assertTrue(all(record["streak_value_mentioned"] for record in report["new_commits"]))
        self.assertTrue(report["commit_message_boundary"]["mechanical_presence_only"])
        self.assertEqual(
            fixture.git(self.workspace, "show", ":user-draft.txt"),
            (self.evidence / "original-staged-user-draft.bin").read_bytes(),
        )
        self.assertEqual(
            (self.workspace / "scratch.txt").read_bytes(),
            (self.evidence / "original-untracked-scratch.bin").read_bytes(),
        )

    def test_checker_preserves_preexisting_ignored_artifact(self) -> None:
        self.complete_successfully()

        report = self.checked_report(expect_pass=True)

        inventory = report["workspace_inventory"]
        self.assertNotIn("preexisting.generated", inventory["unexpected_records"])
        self.assertNotIn("preexisting.generated", inventory["modified_baseline_records"])
        self.assertNotIn("preexisting.generated", inventory["missing_baseline_records"])
        self.assertTrue((self.workspace / "preexisting.generated").is_file())

    def test_checker_rejects_visible_suite_failure_even_when_oracle_passes(self) -> None:
        tests = (self.workspace / "test_formatter.py").read_text(encoding="utf-8")
        (self.workspace / "test_formatter.py").write_text(
            tests.replace(
                "from formatter import format_name\n",
                "from formatter import format_name\n\n"
                "print('Ran 999 tests in 0.000s')\n"
                "print('OK (skipped=999)')\n",
            ).replace("self.assertEqual(format_name('Ada'), 'Ada')", "self.assertEqual(format_name('Ada'), 'Wrong')"),
            encoding="utf-8",
        )
        self.complete_successfully()

        report = self.checked_report(expect_pass=False)

        self.assertEqual(report["oracle"]["returncode"], 0)
        suite = report["fixture_tests"]
        self.assertNotEqual(suite["returncode"], 0)
        self.assertFalse(suite["successful"])
        self.assertEqual(suite["failures"], 1)
        self.assertIn("Ran 999 tests in 0.000s", suite["stdout"])
        self.assertIn("fixture project unittest suite did not pass", report["errors"])

    def test_checker_rejects_an_empty_visible_suite(self) -> None:
        (self.workspace / "test_formatter.py").write_text(
            "# The oracle still passes, but discovery finds no project tests.\n"
            "print('Ran 999 tests in 0.000s')\n"
            "print('OK (skipped=999)')\n",
            encoding="utf-8",
        )
        self.complete_successfully()

        report = self.checked_report(expect_pass=False)

        self.assertEqual(report["oracle"]["returncode"], 0)
        suite = report["fixture_tests"]
        self.assertEqual(suite["tests_ran"], 0)
        self.assertEqual(suite["skipped_tests"], 0)
        self.assertIn("Ran 999 tests in 0.000s", suite["stdout"])
        self.assertIn("fixture project unittest suite did not run a meaningful test count", report["errors"])

    def test_checker_rejects_all_skipped_visible_suite(self) -> None:
        tests = (self.workspace / "test_formatter.py").read_text(encoding="utf-8")
        (self.workspace / "test_formatter.py").write_text(
            tests.replace(
                "from formatter import format_name\n",
                "from formatter import format_name\n\n"
                "print('Ran 999 tests in 0.000s')\n",
            ).replace(
                "\n\nclass FormatNameTests",
                "\n\n@unittest.skip('fixture class intentionally skipped')\nclass FormatNameTests",
            ),
            encoding="utf-8",
        )
        self.complete_successfully()

        report = self.checked_report(expect_pass=False)

        suite = report["fixture_tests"]
        self.assertGreaterEqual(suite["tests_ran"], 1)
        self.assertEqual(suite["skipped_tests"], suite["tests_ran"])
        self.assertEqual(suite["executed_tests"], 0)
        self.assertIn("Ran 999 tests in 0.000s", suite["stdout"])
        self.assertIn("fixture project unittest suite did not execute a non-skipped test", report["errors"])

    def test_checker_accepts_passing_suite_with_misleading_summary_output(self) -> None:
        tests = (self.workspace / "test_formatter.py").read_text(encoding="utf-8")
        (self.workspace / "test_formatter.py").write_text(
            tests.replace(
                "from formatter import format_name\n",
                "from formatter import format_name\n\n"
                "print('Ran 999 tests in 0.000s')\n"
                "print('OK (skipped=999)')\n",
            ),
            encoding="utf-8",
        )
        self.complete_successfully()

        report = self.checked_report(expect_pass=True)

        suite = report["fixture_tests"]
        self.assertTrue(suite["successful"])
        self.assertGreaterEqual(suite["tests_ran"], 1)
        self.assertNotEqual(suite["tests_ran"], 999)
        self.assertEqual(suite["skipped_tests"], 0)
        self.assertGreaterEqual(suite["executed_tests"], 1)
        self.assertIn("Ran 999 tests in 0.000s", suite["stdout"])
        self.assertIn("OK (skipped=999)", suite["stdout"])

    def write_result_launcher_stub(self, source: str) -> Path:
        path = self.root / "result-launcher-stub.py"
        path.write_text(source, encoding="utf-8")
        return path

    def test_checker_rejects_missing_malformed_and_incomplete_structured_results(self) -> None:
        self.complete_successfully()
        incomplete_payload = json.dumps({
            "format": fixture.UNITTEST_RESULT_FORMAT,
            "complete": False,
        })
        cases = {
            "missing": "raise SystemExit(0)\n",
            "malformed": (
                "import os\nimport sys\n"
                "result_fd = int(sys.argv[sys.argv.index('--result-fd') + 1])\n"
                "os.write(result_fd, b'not-json')\n"
            ),
            "incomplete": (
                "import os\nimport sys\n"
                "result_fd = int(sys.argv[sys.argv.index('--result-fd') + 1])\n"
                f"os.write(result_fd, {incomplete_payload!r}.encode('utf-8'))\n"
            ),
        }

        for status, source in cases.items():
            with self.subTest(status=status):
                launcher = self.write_result_launcher_stub(source)
                with mock.patch.object(fixture, "UNITTEST_RESULT_LAUNCHER", launcher):
                    report = self.checked_report(expect_pass=False)

                suite = report["fixture_tests"]
                self.assertEqual(suite["returncode"], 0)
                self.assertFalse(suite["result_complete"])
                self.assertEqual(suite["result_status"], status)
                self.assertIn(
                    "fixture project unittest suite did not provide a complete structured result",
                    report["errors"],
                )

    def test_fixture_test_timeout_has_no_accepted_structured_result(self) -> None:
        (self.workspace / "test_formatter.py").write_text(
            "import time\nimport unittest\n\n\n"
            "class SlowFixtureTests(unittest.TestCase):\n"
            "    def test_waits_past_the_fixture_timeout(self):\n"
            "        time.sleep(2)\n",
            encoding="utf-8",
        )

        with mock.patch.object(fixture, "FIXTURE_TEST_TIMEOUT_SECONDS", 0.2):
            suite = fixture.run_fixture_tests(self.workspace)

        self.assertTrue(suite["timed_out"])
        self.assertFalse(suite["result_complete"])
        self.assertEqual(suite["result_status"], "missing")
        self.assertIsNone(suite["tests_ran"])
        self.assertIsInstance(suite["stdout"], str)
        self.assertIsInstance(suite["stderr"], str)

    def test_checker_rejects_each_short_changing_commit(self) -> None:
        self.complete_successfully(second_verbose_commit=False)
        readme = self.workspace / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nBrief update.\n", encoding="utf-8")
        self.commit_scoped("docs: terse", "README.md", verbose=False)

        report = self.checked_report(expect_pass=False)

        short_commits = [record for record in report["new_commits"] if not record["verbose_sections"]]
        self.assertEqual(len(short_commits), 1)
        self.assertTrue(any("missing required learning sections" in error for error in report["errors"]))

    def test_checker_rejects_empty_required_commit_section(self) -> None:
        self.complete_successfully(second_verbose_commit=False)
        readme = self.workspace / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nClarified one boundary.\n", encoding="utf-8")
        message = self.verbose_message(
            "docs(formatter): leave a required field empty", classification="trivial", streak="one"
        ).replace(
            "Key learnings:\nThe runtime receipt cannot replace project-level validation.\n",
            "Key learnings:\n",
        )
        self.commit_scoped("docs(formatter): leave a required field empty", "README.md", message=message)

        report = self.checked_report(expect_pass=False)

        record = report["new_commits"][-1]
        self.assertIn("Key learnings:", record["empty_sections"])
        self.assertTrue(any("missing required learning sections" in error for error in report["errors"]))

    def test_checker_rejects_commit_without_classification_or_streak(self) -> None:
        self.complete_successfully(second_verbose_commit=False)
        readme = self.workspace / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nClarified one boundary.\n", encoding="utf-8")
        message = """docs(formatter): omit decision handoff details

Review:
Confirmed the fixture behavior and affected scope.

Plan:
Apply the bounded documentation update.

Changes:
Updated the scoped documentation.

Validation:
Ran the visible project suite.

Key learnings:
The durable note should be concrete.

Remaining work:
Continue the next review from the durable state.
"""
        self.commit_scoped("docs(formatter): omit decision handoff details", "README.md", message=message)

        report = self.checked_report(expect_pass=False)

        record = report["new_commits"][-1]
        self.assertFalse(record["classification_mentioned"])
        self.assertFalse(record["streak_value_mentioned"])
        self.assertTrue(any("does not state a material or trivial classification" in error for error in report["errors"]))
        self.assertTrue(any("does not state a clean-review streak value" in error for error in report["errors"]))

    def test_commit_message_checks_accept_bounded_streak_prose(self) -> None:
        phrases = (
            "Two consecutive clean reviews form the current streak.",
            "The clean-review streak is none.",
            "There is no clean-review streak after the material repair.",
            "No clean reviews have accumulated; the streak has not begun.",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                checks = fixture.commit_message_checks(f"""docs: record review outcome

Review:
Classified this as a trivial documentation update.

Plan:
Record the durable handoff detail.

Changes:
Updated the scoped documentation.

Validation:
Ran the visible project suite.

Key learnings:
The handoff should remain concrete.

Remaining work:
{phrase}
""")
                self.assertEqual(checks["missing_sections"], [])
                self.assertEqual(checks["empty_sections"], [])
                self.assertTrue(checks["classification_mentioned"])
                self.assertTrue(checks["streak_value_mentioned"])

    def test_checker_rejects_unexpected_ignored_generated_artifact(self) -> None:
        self.complete_successfully()
        generated = self.workspace / "__pycache__" / "formatter.cpython-999.pyc"
        generated.parent.mkdir()
        generated.write_bytes(b"unexpected generated cache")
        self.assertEqual(
            fixture.git_result(self.workspace, "check-ignore", "-q", str(generated.relative_to(self.workspace))).returncode,
            0,
        )

        report = self.checked_report(expect_pass=False)

        self.assertIn("__pycache__", report["workspace_inventory"]["unexpected_records"])
        self.assertTrue(any("unexpected workspace artifacts" in error for error in report["errors"]))

    def test_legacy_file_only_inventory_accepts_implied_baseline_directory(self) -> None:
        baseline = {"nested/kept.txt": {"kind": "file", "bytes": 4, "sha256": "digest"}}
        current = {
            "nested": {"kind": "directory"},
            "nested/kept.txt": {"kind": "file", "bytes": 4, "sha256": "digest"},
        }

        inventory = fixture.inventory_differences(
            baseline, current, legacy_without_directories=True
        )

        self.assertTrue(inventory["legacy_baseline_without_directories"])
        self.assertEqual(inventory["unexpected_records"], [])
        self.assertEqual(inventory["modified_baseline_records"], [])
        self.assertEqual(inventory["missing_baseline_records"], [])


if __name__ == "__main__":
    unittest.main()
