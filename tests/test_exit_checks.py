"""Frozen exit checks: the runtime runs them on every satisfied report; failed checks can be repaired, never their clauses."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "until_loop_ephemeral.py"


def call(argv, payload=None):
    p = subprocess.run([sys.executable, *argv], input=None if payload is None else json.dumps(payload),
                       capture_output=True, text=True)
    return json.loads(p.stdout)


def report(exit_assessment="satisfied", continuation="allowed", classification="non-trivial"):
    return {"classification": classification, "exit_assessment": exit_assessment,
            "continuation_assessment": continuation, "evidence": "observed", "handoff": "facts"}


class ExitChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, checks):
        contract = {"workspace": str(self.ws), "work": "w", "exit_condition": "e", "repeat_condition": "r",
                    "context": {"request": "q", "scope": "s", "authority": "a", "environment": "e", "resources": []},
                    "exit_checks": checks}
        return call([str(SCRIPT), "start"], contract)

    def done(self, packet, rep):
        return call(packet["done_argv"][1:], rep)

    def test_passing_checks_complete(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag"}])
        (self.ws / "flag").write_text("1")
        out = self.done(pk, report())
        self.assertEqual(out["status"], "complete")
        self.assertTrue(out["last_check_results"][0]["passed"])

    def test_failing_check_refuses_completion_and_names_it(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag || echo missing-flag; test -f flag"}])
        out = self.done(pk, report())
        self.assertEqual(out["status"], "active")
        self.assertFalse(out["last_check_results"][0]["passed"])
        self.assertIn("X1", out["instruction"])
        self.assertIn("missing-flag", out["instruction"])
        (self.ws / "flag").write_text("1")
        self.assertEqual(self.done(out, report())["status"], "complete")

    def test_unsatisfied_claim_runs_no_checks(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "touch ran"}])
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        self.assertEqual(out["status"], "active")
        self.assertFalse((self.ws / "ran").exists())

    def test_cancelled_stops_without_checks(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "touch ran; false"}])
        out = self.done(pk, report(continuation="cancelled"))
        self.assertEqual(out["status"], "stopped")
        self.assertFalse((self.ws / "ran").exists())

    def test_blocked_with_failing_check_stops_incomplete(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        out = self.done(pk, report(continuation="blocked"))
        self.assertEqual(out["status"], "stopped")

    def test_no_checks_behaves_as_before(self):
        out = self.done(self.start([]), report())
        self.assertEqual(out["status"], "complete")

    def test_repair_of_failed_check_reruns_and_is_recorded(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flagg"}])
        (self.ws / "flag").write_text("1")
        out = self.done(pk, report())
        self.assertEqual(out["status"], "active")
        rep = dict(report(), check_repairs=[{"id": "X1", "command": "test -f flag", "reason": "typo in file name"}])
        out = self.done(out, rep)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["check_repairs"][0]["old_command"], "test -f flagg")
        self.assertEqual(out["exit_checks"][0]["clause"], "flag exists")

    def test_repair_of_unfailed_check_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        rep = dict(report(exit_assessment="unsatisfied"), check_repairs=[{"id": "X1", "command": "true", "reason": "r"}])
        self.assertEqual(self.done(pk, rep)["status"], "error")

    def test_third_repair_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        out = self.done(pk, report())
        for n in range(2):
            out = self.done(out, dict(report(), check_repairs=[{"id": "X1", "command": "false", "reason": f"r{n}"}]))
            self.assertEqual(out["status"], "active")
        self.assertEqual(self.done(out, dict(report(), check_repairs=[{"id": "X1", "command": "true", "reason": "r3"}]))["status"], "error")

    def test_bad_check_schema_refused(self):
        out = self.start([{"id": "X1", "command": "true"}])
        self.assertEqual(out["status"], "error")


if __name__ == "__main__":
    unittest.main()
