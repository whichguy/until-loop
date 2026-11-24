"""Frozen exit checks run by the executor's own shell (verify_script), recorded by the runtime through a pipe.

The host's shell tool runs verify_script, so its permissions and hooks see every check command; the script pipes
the real output into `record`, and `done` accepts a satisfied report only with a fresh recorded run for the
current action. Failed checks can be repaired, never their clauses."""
import json
from itertools import product
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
    """What the executor does: run verify_script unchanged with a shell."""
    return subprocess.run(["/bin/sh", "-c", packet["verify_script"]], capture_output=True, text=True, errors="replace")


class Harness(unittest.TestCase):
    """Shared setup only: no tests here, so subclasses do not re-run each other's tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def git_init(self):
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", "--allow-empty"]):
            subprocess.run(["git", *args], cwd=self.ws, check=True, capture_output=True)

    def start(self, checks, reviews=0):
        contract = {"workspace": str(self.ws), "work": "w", "exit_condition": "e", "repeat_condition": "r",
                    "required_trivial_reviews": reviews,
                    "context": {"request": "q", "scope": "s", "authority": "a", "environment": "e", "resources": []},
                    "exit_checks": checks}
        return call([str(SCRIPT), "start", "--isolated"], contract)

    def done(self, packet, rep):
        return call(packet["done_argv"][1:], rep)


class ExitChecks(Harness):
    def test_every_combination_of_five_registered_checks(self):
        names = ["scope", "format", "checksum", "schema", "manifest"]
        packet = self.start([{"id": name, "clause": f"{name} exists", "command": f"test -f {name}"}
                             for name in names])
        for outcomes in product((False, True), repeat=len(names)):
            with self.subTest(outcomes=outcomes):
                for name, ready in zip(names, outcomes):
                    if ready:
                        (self.ws / name).touch()
                    else:
                        (self.ws / name).unlink(missing_ok=True)
                verification = run_verify(packet)
                self.assertEqual(verification.returncode, 0, verification.stderr)
                packet = self.done(packet, report())
                self.assertEqual([(row["id"], row["passed"]) for row in packet["last_check_results"]],
                                 list(zip(names, outcomes)))
                self.assertEqual(packet["status"], "complete" if all(outcomes) else "active")

    def test_script_shows_every_command_and_records_results(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag"},
                         {"id": "X2", "clause": "always", "command": "true"}])
        self.assertIn("test -f flag", pk["verify_script"])
        out = run_verify(pk)
        self.assertIn("UNTIL-LOOP-CHECK X1 exit=1", out.stdout)
        self.assertIn("UNTIL-LOOP-RECORDED 2 checks for this action: 1 failed (X1)", out.stdout)

    def test_no_checks_has_no_script_and_behaves_as_before(self):
        pk = self.start([])
        self.assertIsNone(pk["verify_script"])
        self.assertEqual(self.done(pk, report())["status"], "complete")

    def test_satisfied_without_a_recorded_run_is_refused_unchanged(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        out = self.done(pk, report())
        self.assertEqual(out["status"], "error")
        self.assertIn("verify_script", out["error"])
        run_verify(pk)
        self.assertEqual(self.done(pk, report())["status"], "complete")

    def test_typed_results_are_not_accepted(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        rep = report(check_results=[{"id": "X1", "exit_code": 0, "output_tail": ""}])
        self.assertEqual(self.done(pk, rep)["status"], "error")

    def test_failing_check_refuses_completion_and_names_it(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag || echo missing-flag; test -f flag"}])
        run_verify(pk)
        out = self.done(pk, report())
        self.assertEqual(out["status"], "active")
        self.assertFalse(out["last_check_results"][0]["passed"])
        self.assertIn("missing-flag", out["instruction"])
        (self.ws / "flag").write_text("1")
        run_verify(out)
        self.assertEqual(self.done(out, report())["status"], "complete")

    def test_a_run_recorded_in_an_earlier_action_is_stale(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        run_verify(pk)
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        self.assertEqual(self.done(out, report())["status"], "error")

    def test_workspace_change_after_the_recorded_run_is_refused(self):
        self.git_init()
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        run_verify(pk)
        (self.ws / "late-edit.txt").write_text("changed after verify")
        out = self.done(pk, report())
        self.assertEqual(out["status"], "error")
        self.assertIn("changed after the recorded verify run", out["error"])
        run_verify(pk)
        self.assertEqual(self.done(pk, report())["status"], "complete")

    def test_record_with_a_stale_token_records_nothing(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        stale = run_verify(pk)  # the old packet's script carries the previous action's token
        self.assertIn("UNTIL-LOOP-RECORD-ERROR", stale.stdout)
        self.assertEqual(self.done(out, report())["status"], "error")

    def test_cancelled_stops_without_a_run(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        self.assertEqual(self.done(pk, report(continuation="cancelled"))["status"], "stopped")

    def test_blocked_with_failing_check_stops_incomplete(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        run_verify(pk)
        self.assertEqual(self.done(pk, report(continuation="blocked"))["status"], "stopped")

    def test_failed_check_never_counts_toward_the_review_gate(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}], reviews=2)
        run_verify(pk)
        out = self.done(pk, report(classification="trivial"))
        self.assertEqual(out["progress"]["trivial_streak"], 0)

    def test_repair_flow_is_recorded_and_listed(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flagg"}])
        (self.ws / "flag").write_text("1")
        run_verify(pk)
        out = self.done(pk, report())
        self.assertEqual(out["status"], "active")
        fix = [{"id": "X1", "command": "test -f flag", "reason": "typo"}]
        self.assertEqual(self.done(out, report(check_repairs=fix))["status"], "error")  # never with satisfied
        out = self.done(out, report(exit_assessment="unsatisfied", check_repairs=fix))
        self.assertIn("test -f flag\n", out["verify_script"])
        run_verify(out)
        out = self.done(out, report())
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["check_repairs"][0]["old_command"], "test -f flagg")
        self.assertEqual(out["exit_checks"][0]["clause"], "flag exists")
        self.assertIn("list each repair", out["instruction"])

    def test_repair_of_unfailed_check_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        rep = report(exit_assessment="unsatisfied", check_repairs=[{"id": "X1", "command": "true", "reason": "r"}])
        self.assertEqual(self.done(pk, rep)["status"], "error")

    def test_third_repair_refused(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        run_verify(pk)
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        for n in range(2):
            out = self.done(out, report(exit_assessment="unsatisfied",
                                        check_repairs=[{"id": "X1", "command": "false", "reason": f"r{n}"}]))
            self.assertEqual(out["status"], "active")
        rep = report(exit_assessment="unsatisfied", check_repairs=[{"id": "X1", "command": "true", "reason": "r3"}])
        self.assertEqual(self.done(out, rep)["status"], "error")

    def test_huge_and_binary_output_is_bounded(self):
        pk = self.start([{"id": "X1", "clause": "c",
                          "command": "head -c 3000000 /dev/zero | tr '\\0' 'a'; printf '\\377\\376'; exit 3"},
                         {"id": "X2", "clause": "d", "command": "echo noise; true"}])
        run_verify(pk)
        out = self.done(pk, report())
        r = {x["id"]: x for x in out["last_check_results"]}
        self.assertEqual(r["X1"]["exit_code"], 3)
        self.assertLessEqual(len(r["X1"]["output_tail"]), 300)
        self.assertEqual(r["X2"]["output_tail"], "")

    def test_a_check_cannot_read_the_verify_pipe(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "cat >/dev/null; true"}, {"id": "X2", "clause": "d", "command": "true"}])
        out = run_verify(pk)
        self.assertIn("all passed", out.stdout)

    def test_many_checks_and_a_huge_contract_are_not_refused_for_size(self):
        checks = [{"id": f"X{i}", "clause": "c" * 2000, "command": f"test {i} -ge 0"} for i in range(60)]
        pk = self.start(checks)
        self.assertEqual(pk["status"], "active")
        out = run_verify(pk)
        self.assertIn("60 checks for this action: all passed", out.stdout)
        self.assertEqual(self.done(pk, report())["status"], "complete")

    def test_nul_in_command_refused(self):
        self.assertEqual(self.start([{"id": "X1", "clause": "c", "command": "true\x00"}])["status"], "error")

    def test_bad_check_schema_refused(self):
        self.assertEqual(self.start([{"id": "X1", "command": "true"}])["status"], "error")


if __name__ == "__main__":
    unittest.main()


class ReviewRound2(Harness):
    """Findings of the second adversarial review (GPT-6.1 Sol), each pinned by a test."""

    def test_trivial_failed_report_then_passing_record_is_accepted(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f flag"}], reviews=1)
        run_verify(pk)
        out = self.done(pk, report(classification="trivial"))
        self.assertEqual(out["progress"]["trivial_streak"], 0)
        (self.ws / "flag").write_text("1")
        rec = run_verify(out)
        self.assertIn("all passed", rec.stdout)
        self.assertEqual(self.done(out, report(classification="trivial"))["status"], "complete")

    def test_unparseable_command_refused_at_start_and_in_a_repair(self):
        out = self.start([{"id": "X1", "clause": "c", "command": "test -f 'flag"}])
        self.assertEqual(out["status"], "error")
        self.assertIn("not valid sh syntax", out["error"])
        pk = self.start([{"id": "X1", "clause": "c", "command": "false"}])
        run_verify(pk)
        out = self.done(pk, report(exit_assessment="unsatisfied"))
        bad = report(exit_assessment="unsatisfied", check_repairs=[{"id": "X1", "command": "test -f 'x", "reason": "r"}])
        self.assertEqual(self.done(out, bad)["status"], "error")

    def test_check_ids_must_be_simple_tokens(self):
        self.assertEqual(self.start([{"id": "flag exists", "clause": "c", "command": "true"}])["status"], "error")

    def test_script_runs_under_sh_whatever_the_host_shell(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": 'status=ok; test "$status" = ok'},
                         {"id": "X2", "clause": "d", "command": "false | true"}])
        for shell in (["/bin/zsh", "-c"], ["/bin/bash", "-o", "pipefail", "-c"]):
            if not Path(shell[0]).exists():
                continue
            out = subprocess.run([*shell, pk["verify_script"]], capture_output=True, text=True, errors="replace")
            self.assertIn("all passed", out.stdout, (shell, out.stdout, out.stderr))

    def test_late_recorder_cannot_overwrite_a_newer_action(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import until_loop_ephemeral as rt
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        path, token = pk["state_file"], pk["done_argv"][-1].split("=", 1)[1]

        class Advancing:
            def __init__(self):
                self.sent = False

            def read(self, n):
                if not self.sent:
                    self.sent = True
                    rt.done(path, token, report(exit_assessment="unsatisfied"))
                    return b"UNTIL-LOOP-CHECK X1 exit=0\n"
                return b""

        import io
        with self.assertRaises(rt.StateError):
            rt.record(path, token, Advancing(), io.StringIO())
        self.assertEqual(rt.read_state(path)["action_number"], 2)

    def test_newline_free_huge_output_is_parsed_in_bounded_lines(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import io
        import until_loop_ephemeral as rt
        lines = list(rt._bounded_lines(io.BytesIO(b"a" * 5_000_000 + b"\nUNTIL-LOOP-CHECK X1 exit=0\n")))
        self.assertLessEqual(max(len(l) for l in lines), 8193)
        self.assertEqual(lines[-1], "UNTIL-LOOP-CHECK X1 exit=0\n")


class LeanPacketTests(Harness):
    """A contract without exit checks gets no verify/repair text (Improve and ShipLoop contracts carry none)."""

    def test_no_checks_means_no_verify_or_repair_text(self):
        pk = self.start([])
        self.assertNotIn("verify_script", pk["instruction"])
        self.assertNotIn("check_repairs", json.dumps(pk["report_schema"]))
        self.assertIsNone(pk["verify_script"])

    def test_with_checks_the_packet_carries_both(self):
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        self.assertIn("verify_script", pk["instruction"])
        self.assertIn("check_repairs", pk["report_schema"]["properties"])


class VerifyConsistencyTests(Harness):
    def test_workspace_change_during_verify_is_refused_and_recoverable(self):
        (self.ws / "tracked.txt").write_text("clean", encoding="utf-8")
        self.git_init()
        pk = self.start(
            [{"id": "X1", "clause": "tracked file stays quiet",
              "command": "if [ ! -f quiet ]; then printf dirty > tracked.txt; fi; true"}],
            reviews=2,
        )
        rec = run_verify(pk)
        self.assertEqual(rec.returncode, 2, rec.stdout + rec.stderr)
        self.assertIn("UNTIL-LOOP-RECORD-ERROR", rec.stdout)
        self.assertIn("changed during the verify run", rec.stdout)

        err = self.done(pk, report(classification="trivial"))
        self.assertEqual(err["status"], "error")
        self.assertEqual(err["state_change"], "unchanged")
        self.assertIn("changed during the recorded verify run", err["error"])

        active = self.done(pk, report(classification="trivial", exit_assessment="unsatisfied"))
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["progress"]["trivial_streak"], 0)

        (self.ws / "quiet").write_text("1", encoding="utf-8")
        rec2 = run_verify(active)
        self.assertEqual(rec2.returncode, 0, rec2.stdout + rec2.stderr)
        self.assertIn("all passed", rec2.stdout)
        step2 = self.done(active, report(classification="trivial", exit_assessment="unsatisfied"))
        self.assertEqual(step2["status"], "active")
        self.assertEqual(step2["progress"]["trivial_streak"], 1)

        rec3 = run_verify(step2)
        self.assertEqual(rec3.returncode, 0, rec3.stdout + rec3.stderr)
        terminal = self.done(step2, report(classification="trivial", exit_assessment="satisfied"))
        self.assertEqual(terminal["status"], "complete")

    def test_spoofed_tree_before_line_in_check_output_is_ignored(self):
        self.git_init()
        clean_pk = self.start([{
            "id": "X1",
            "clause": "clean check prints misleading marker",
            "command": "printf 'UNTIL-LOOP-TREE-BEFORE %s\\n' 0000000000000000000000000000000000000000",
        }])
        clean_rec = run_verify(clean_pk)
        self.assertEqual(clean_rec.returncode, 0, clean_rec.stdout + clean_rec.stderr)
        self.assertIn("UNTIL-LOOP-TREE-BEFORE 0000000000000000000000000000000000000000", clean_rec.stdout)
        self.assertEqual(self.done(clean_pk, report())["status"], "complete")

        mutating_pk = self.start([{
            "id": "X1",
            "clause": "mutating check spoofs new tree",
            "command": (
                "printf dirty > mutated.txt && "
                "git add mutated.txt && "
                "printf 'UNTIL-LOOP-TREE-BEFORE %s\\n' \"$(git write-tree)\""
            ),
        }])
        mutating_rec = run_verify(mutating_pk)
        self.assertEqual(mutating_rec.returncode, 2, mutating_rec.stdout + mutating_rec.stderr)
        self.assertIn("UNTIL-LOOP-RECORD-ERROR", mutating_rec.stdout)
        self.assertEqual(self.done(mutating_pk, report())["status"], "error")

    def test_snapshot_cli_in_git_and_non_git_workspace(self):
        non_git = subprocess.run(
            [sys.executable, str(SCRIPT), "snapshot", "--workspace", str(self.ws)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(non_git.stdout, "UNTIL-LOOP-TREE-BEFORE none\n")

        self.git_init()
        first = subprocess.run(
            [sys.executable, str(SCRIPT), "snapshot", "--workspace", str(self.ws)],
            capture_output=True, text=True, check=True,
        )
        self.assertRegex(first.stdout, r"^UNTIL-LOOP-TREE-BEFORE [0-9a-f]{40,64}\n$")

        (self.ws / "extra.txt").write_text("x", encoding="utf-8")
        second = subprocess.run(
            [sys.executable, str(SCRIPT), "snapshot", "--workspace", str(self.ws)],
            capture_output=True, text=True, check=True,
        )
        self.assertRegex(second.stdout, r"^UNTIL-LOOP-TREE-BEFORE [0-9a-f]{40,64}\n$")
        self.assertNotEqual(first.stdout, second.stdout)

    def test_verify_record_schema_validates_changed_flag(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import until_loop_ephemeral as rt
        pk = self.start([{"id": "X1", "clause": "c", "command": "true"}])
        state = rt.read_state(pk["state_file"])
        valid = {**state, "verify_record": {"action_number": 1, "tree": None, "changed": True}}
        self.assertTrue(rt._validate_state(valid)["verify_record"]["changed"])
        for bad_record in (
            {"action_number": 1, "tree": None, "changed": "true"},
            {"action_number": 1, "tree": None, "changed": 1},
            {"action_number": True, "tree": None},
        ):
            with self.subTest(bad_record=bad_record):
                with self.assertRaises(rt.StateError):
                    rt._validate_state({**state, "verify_record": bad_record})

    def test_multistatement_check_fails_closed_under_set_e(self):
        for cmd in ("false\ntrue", "false; true"):
            with self.subTest(cmd=cmd):
                pk = self.start([{"id": "M1", "clause": "both succeed", "command": cmd}])
                rec = run_verify(pk)
                self.assertEqual(rec.returncode, 0, rec.stdout + rec.stderr)
                out = self.done(pk, report())
                self.assertEqual(out["status"], "active")
                self.assertEqual(out["last_check_results"][0]["exit_code"], 1)
                self.assertFalse(out["last_check_results"][0]["passed"])

        capture_pk = self.start([{
            "id": "M2",
            "clause": "expected nonzero exit code 2",
            "command": 'rc=0; (exit 2) || rc=$?; test "$rc" -eq 2',
        }, {
            "id": "M3",
            "clause": "fallback log still fails final test",
            "command": "test -f flag || echo missing; test -f flag",
        }])
        run_verify(capture_pk)
        out = self.done(capture_pk, report())
        self.assertEqual(out["status"], "active")
        self.assertEqual([(r["id"], r["passed"]) for r in out["last_check_results"]],
                         [("M2", True), ("M3", False)])
        (self.ws / "flag").write_text("1", encoding="utf-8")
        run_verify(out)
        self.assertEqual(self.done(out, report())["status"], "complete")

    def test_vacuous_check_repairs_are_refused(self):
        pk = self.start([{"id": "X1", "clause": "flag exists", "command": "test -f wrong_flag"}])
        run_verify(pk)
        failed = self.done(pk, report(exit_assessment="unsatisfied"))
        self.assertEqual(failed["status"], "active")

        vacuous_commands = (
            "true",
            ":",
            "exit 0",
            "exit",
            "# bypass check\ntrue",
            "test -f flag || true",
            "test -f flag || :",
            "test -f flag || exit 0",
            "test -f flag; true",
            "test -f flag\n:",
            "test -f flag || (true)",
            "test -f flag || { echo ok; true; }",
        )
        for bad_cmd in vacuous_commands:
            with self.subTest(bad_cmd=bad_cmd):
                err = self.done(failed, report(
                    exit_assessment="unsatisfied",
                    check_repairs=[{"id": "X1", "command": bad_cmd, "reason": "bypass"}],
                ))
                self.assertEqual(err["status"], "error")
                self.assertEqual(err["state_change"], "unchanged")
                self.assertIn("vacuous", err["error"])

        repaired = self.done(failed, report(
            exit_assessment="unsatisfied",
            check_repairs=[{"id": "X1", "command": "test -f flag", "reason": "match clause name"}],
        ))
        self.assertEqual(repaired["status"], "active")
        (self.ws / "flag").write_text("1", encoding="utf-8")
        run_verify(repaired)
        terminal = self.done(repaired, report())
        self.assertEqual(terminal["status"], "complete")
