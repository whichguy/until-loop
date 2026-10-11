"""Frozen exit checks: the executor runs the packet's verify_script with its own shell tool (so host permissions
and hooks see every check) and reports check_results; the runtime refuses a satisfied report without them and
refuses completion while any check failed. Failed checks can be repaired, never their clauses."""
import json
import re
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


def report(exit_assessment="satisfied", continuation="allowed", classification="non-trivial", **extra):
    return {"classification": classification, "exit_assessment": exit_assessment,
            "continuation_assessment": continuation, "evidence": "observed", "handoff": "facts", **extra}


def run_verify(packet):
    """What an executor does: run verify_script with a shell and copy its result lines."""
    p = subprocess.run(["/bin/sh", "-c", packet["verify_script"]], capture_output=True, text=True)
    return [{"id": m.group(1), "exit_code": int(m.group(2)), "output_tail": "" if m.group(2) == "0" else p.stdout[-200:]}
            for m in re.finditer(r"^UNTIL-LOOP-CHECK (\S+) exit=(\d+)$", p.stdout, re.M)]


class ExitChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, checks, reviews=0):
        contract = {"workspace": str(self.ws), "work": "w", "exit_condition": "e", "repeat_condition": "r",
                    "required_trivial_reviews": reviews,
                    "context": {"request": "q", "scope": "s", "authority": "a", "environment": "e", "resources": []},
                    "exit_checks": checks}
        return call([str(SCRIPT), "start"], contract)

    def done(self, packet, rep):
        return call(packet["done_argv"][1:], rep)

    def verified(self, packet, **kw):
        return report(check_results=run_verify(packet), **kw)

    def test_verify_script_contains_every_command_and_runs_in_the_workspace(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag"},
                         {"id": "X2", "clause": "pwd", "command": "test \"$(pwd -P)\" = \"$(cd \"$PWD\" && pwd -P)\" && test -d ."}])
        self.assertIn("test -f flag", pk["verify_script"])
        (self.ws / "flag").write_text("1")
        self.assertEqual([r["exit_code"] for r in run_verify(pk)], [0, 0])

    def test_no_checks_has_no_script_and_behaves_as_before(self):
        pk = self.start([])
        self.assertIsNone(pk["verify_script"])
        self.assertEqual(self.done(pk, report())["status"], "complete")

    def test_satisfied_without_results_is_refused_unchanged(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        out = self.done(pk, report())
        self.assertEqual(out["status"], "error")
        self.assertIn("verify_script", out["error"])
        self.assertEqual(self.done(pk, self.verified(pk))["status"], "complete")

    def test_failing_result_refuses_completion_and_names_it(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag || echo missing-flag; test -f flag"}])
        out = self.done(pk, self.verified(pk))
        self.assertEqual(out["status"], "active")
        self.assertFalse(out["last_check_results"][0]["passed"])
        self.assertIn("X1", out["instruction"])
        (self.ws / "flag").write_text("1")
        self.assertEqual(self.done(out, self.verified(out))["status"], "complete")

    def test_results_must_cover_every_check_exactly(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}, {"id": "X2", "clause": "d", "command": "true"}])
        for bad in ([{"id": "X1", "exit_code": 0, "output_tail": ""}],
                    [{"id": "X1", "exit_code": 0, "output_tail": ""}, {"id": "X9", "exit_code": 0, "output_tail": ""}],
                    [{"id": "X1", "exit_code": True, "output_tail": ""}, {"id": "X2", "exit_code": 0, "output_tail": ""}]):
            self.assertEqual(self.done(pk, report(check_results=bad))["status"], "error")

    def test_unsatisfied_needs_no_results_but_records_them(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        self.assertEqual(out["status"], "active")
        out = self.done(out, self.verified(out, exit_assessment="unsatisfied"))
        self.assertFalse(out["last_check_results"][0]["passed"])

    def test_cancelled_stops_without_results(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        self.assertEqual(self.done(pk, report(continuation="cancelled"))["status"], "stopped")

    def test_blocked_with_failing_result_stops_incomplete(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        self.assertEqual(self.done(pk, self.verified(pk, continuation="blocked"))["status"], "stopped")

    def test_failed_check_never_counts_toward_the_review_gate(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}], reviews=2)
        out = self.done(pk, self.verified(pk, classification="trivial"))
        self.assertEqual(out["progress"]["trivial_streak"], 0)

    def test_repair_of_failed_check_is_recorded_and_listed(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flagg"}])
        (self.ws / "flag").write_text("1")
        out = self.done(pk, self.verified(pk))
        self.assertEqual(out["status"], "active")
        repaired = [{"id": "X1", "exit_code": 0, "output_tail": ""}]  # the executor ran the repaired command
        out = self.done(out, report(check_repairs=[{"id": "X1", "command": "test -f flag", "reason": "typo"}],
                                    check_results=repaired))
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["check_repairs"][0]["old_command"], "test -f flagg")
        self.assertEqual(out["exit_checks"][0]["clause"], "flag exists")
        self.assertIn("list each repair", out["instruction"])

    def test_repair_after_an_unsatisfied_report_keeps_eligibility(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flagg"}])
        (self.ws / "flag").write_text("1")
        out = self.done(pk, self.verified(pk))
        out = self.done(out, report(exit_assessment="unsatisfied"))
        self.assertIn("test -f flagg", out["verify_script"])
        out = self.done(out, report(check_repairs=[{"id": "X1", "command": "test -f flag", "reason": "typo"}],
                                    exit_assessment="unsatisfied"))
        self.assertIn("test -f flag\n", out["verify_script"])
        self.assertEqual(self.done(out, self.verified(out))["status"], "complete")

    def test_repair_of_unfailed_check_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        rep = report(exit_assessment="unsatisfied", check_repairs=[{"id": "X1", "command": "true", "reason": "r"}])
        self.assertEqual(self.done(pk, rep)["status"], "error")

    def test_third_repair_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        out = self.done(pk, self.verified(pk))
        for n in range(2):
            out = self.done(out, self.verified(out, check_repairs=[{"id": "X1", "command": "false", "reason": f"r{n}"}]))
            self.assertEqual(out["status"], "active")
        rep = report(check_repairs=[{"id": "X1", "command": "true", "reason": "r3"}],
                     check_results=[{"id": "X1", "exit_code": 0, "output_tail": ""}])
        self.assertEqual(self.done(out, rep)["status"], "error")

    def test_failed_output_tail_is_bounded_and_passes_store_none(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}, {"id": "X2", "clause": "d", "command": "true"}])
        out = self.done(pk, report(check_results=[{"id": "X1", "exit_code": 3, "output_tail": "x" * 5000},
                                                  {"id": "X2", "exit_code": 0, "output_tail": "noise"}]))
        r = {x["id"]: x for x in out["last_check_results"]}
        self.assertEqual(len(r["X1"]["output_tail"]), 300)
        self.assertEqual(r["X2"]["output_tail"], "")

    def test_nul_in_command_refused(self):
        self.assertEqual(self.start([{"id": "X1", "clause": "c", "command": "true\x00"}])["status"], "error")

    def test_bad_check_schema_refused(self):
        self.assertEqual(self.start([{"id": "X1", "command": "true"}])["status"], "error")


if __name__ == "__main__":
    unittest.main()
