"""Exercise registered check composition and declared stops through the CLI."""

from pathlib import Path
import unittest

import test_exit_checks as exit_checks


class ConditionCompositionTests(exit_checks.Harness):
    def start_contract(self, checks, *, condition, repeat="Continue while useful work is allowed."):
        contract = {
            "workspace": str(self.ws),
            "work": "Qualify the fixture.",
            "exit_condition": condition,
            "repeat_condition": repeat,
            "required_trivial_reviews": 0,
            "context": {
                "request": f"{condition} {repeat}", "scope": "Fixture only", "authority": "Local checks",
                "environment": "Temporary workspace", "resources": [],
            },
            "exit_checks": checks,
        }
        packet = exit_checks.call([str(exit_checks.SCRIPT), "start", "--isolated", "--directory", str(self.ws)],
                                  contract)
        self.assertEqual(packet["status"], "active", packet)
        return packet

    def finish_checks(self, packet, expected, *, continuation="allowed", evidence="observed"):
        verification = exit_checks.run_verify(packet)
        self.assertEqual(verification.returncode, 0, (verification.stdout, verification.stderr))
        outcome = self.done(packet, exit_checks.report(continuation=continuation, evidence=evidence))
        self.assertEqual([(row["id"], row["passed"]) for row in outcome["last_check_results"]], expected)
        return outcome

    def test_eight_and_twelve_checks_block_scattered_failures_until_all_pass(self):
        for size, failures in ((8, {2, 5, 8}), (12, {1, 7, 12})):
            with self.subTest(size=size):
                check_ids = [f"C{size}-{position}" for position in range(1, size + 1)]
                cases = [(check_id, position not in failures) for position, check_id in enumerate(check_ids, 1)]
                checks = [{"id": check_id, "clause": f"{check_id} exists.", "command": f"test -f {check_id}"}
                          for check_id in check_ids]
                for check_id, ready in cases:
                    if ready:
                        (self.ws / check_id).touch()
                packet = self.start_contract(checks, condition="Every named readiness file exists.")
                blocked = self.finish_checks(packet, cases)
                self.assertEqual(blocked["status"], "active")
                for check_id in check_ids:
                    (self.ws / check_id).touch()
                result = self.finish_checks(blocked, [(check_id, True) for check_id in check_ids])
                self.assertEqual(result["status"], "complete")

    def test_one_check_accepts_each_of_five_alternatives(self):
        branches = [f"output-{number}" for number in range(1, 6)]
        condition = f"At least one of {', '.join(branches)} exists."
        check = {"id": "ANY", "clause": condition, "command": " || ".join(f"test -f {b}" for b in branches)}
        packet = self.start_contract([check], condition=condition)
        result = self.finish_checks(packet, [("ANY", False)])
        self.assertEqual(result["status"], "active")
        for index, branch in enumerate(branches):
            with self.subTest(branch=branch):
                if index:
                    result = self.start_contract([check], condition=condition)
                marker = self.ws / branch
                marker.touch()
                try:
                    result = self.finish_checks(result, [("ANY", True)])
                    self.assertEqual(result["status"], "complete")
                finally:
                    marker.unlink()

    def test_each_declared_cancellation_stops_with_green_checks(self):
        stops = ("access-lost", "window-closed", "budget-exhausted", "authority-revoked", "owner-cancelled")
        repeat = f"Stop incomplete immediately if any of these markers exists: {', '.join(stops)}."
        check_ids = [f"READY{number}" for number in range(1, 7)]
        checks = [{"id": check_id, "clause": f"{check_id} exists.", "command": f"test -f {check_id}"}
                  for check_id in check_ids]
        for check_id in check_ids:
            (self.ws / check_id).touch()
        for stop in stops:
            marker = self.ws / stop
            marker.touch()
            try:
                # The caller supplies the assessment in both arms with identical evidence.
                for continuation, status in (("cancelled", "stopped"), ("allowed", "complete")):
                    with self.subTest(stop=stop, continuation=continuation):
                        packet = self.start_contract(checks, condition="All readiness markers exist.", repeat=repeat)
                        result = self.finish_checks(packet, [(check_id, True) for check_id in check_ids],
                                                    continuation=continuation, evidence=f"{stop} is present.")
                        self.assertEqual(result["status"], status)
                        self.assertEqual(result["last_report"]["continuation_assessment"], continuation)
                        self.assertFalse(Path(result["state_file"]).exists())
            finally:
                marker.unlink()


if __name__ == "__main__":
    unittest.main()
