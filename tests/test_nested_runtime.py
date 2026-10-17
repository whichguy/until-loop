"""Small nested-call scenarios through the real CLI, receipts and shell checks.

The test acts as a synchronous host: suspend a parent action, execute a child,
then resume the parent's own packet. This exercises the protocol, not a model's
ability to follow the skill card or a host's slash-command dispatcher.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/until_loop_ephemeral.py"


class NestedLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="until-loop-nested-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "shared workspace"
        self.states = self.root / "private states"
        self.receipts = self.root / "receipts"
        for directory in (self.workspace, self.states, self.receipts):
            directory.mkdir()
        self.script = SCRIPT
        self.environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def command(self, argv, *, stdin=None, returncode=0):
        result = subprocess.run(
            [str(arg) for arg in argv], input=stdin, text=True,
            capture_output=True, cwd=self.root, env=self.environment, timeout=30,
        )
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        return result

    def invoke(self, argv, payload=None, *, returncode=0):
        result = self.command(
            argv, stdin=None if payload is None else json.dumps(payload),
            returncode=returncode,
        )
        return json.loads(result.stdout)

    def receipt(self, name):
        return self.receipts / (name + ".json")

    def start(self, name, *, check=None, reviews=0, resources=(), receipt=None, returncode=0):
        contract = {
            "workspace": str(self.workspace),
            "work": f"Perform one assigned {name} iteration after its dependencies and check its artifact.",
            "exit_condition": f"The required work for {name} has current artifact evidence.",
            "repeat_condition": "Continue while useful authorized work remains; stop on an unmet dependency.",
            "required_trivial_reviews": reviews,
            "context": {
                "request": f"Complete {name} and verify its artifact after its dependencies.",
                "scope": f"Only the temporary workspace and {name}'s assigned artifacts.",
                "authority": "Local fixture writes and checks only; no external actions.",
                "environment": "Use the emitted CLI callbacks and POSIX shell exit checks.",
                "resources": list(resources),
            },
            # Reusing the same check ID across runs must not share recorded results.
            "exit_checks": [{"id": "artifact", "clause": f"{name} artifact is ready",
                             "command": check or f"test -f {name}.txt"}],
        }
        # Exercise the parent-written contract transport: the file is piped unchanged.
        contract_file = self.root / (name + "-contract.json")
        contract_file.write_text(json.dumps(contract), encoding="utf-8")
        result = self.command(
            [sys.executable, self.script, "start", "--directory", self.states,
             "--receipt", receipt or self.receipt(name)],
            stdin=contract_file.read_text(encoding="utf-8"), returncode=returncode,
        )
        return json.loads(result.stdout)

    def saved(self, name):
        return json.loads(self.receipt(name).read_text(encoding="utf-8"))

    def refresh(self, name):
        return self.invoke(self.saved(name)["next_argv"])

    def verify(self, packet, *, passed=True):
        result = self.command(["/bin/sh", "-c", packet["verify_script"]])
        outcome = "all passed" if passed else "1 failed (artifact)"
        self.assertIn("UNTIL-LOOP-RECORDED 1 checks for this action: " + outcome, result.stdout)

    def finish(self, packet, *, assessment="satisfied", continuation="allowed",
               classification="non-trivial", evidence="The assigned artifact was written and checked.",
               handoff="The artifact and this run's receipt retain the result.", returncode=0):
        return self.invoke(packet["done_argv"], {
            "classification": classification, "exit_assessment": assessment,
            "continuation_assessment": continuation, "evidence": evidence, "handoff": handoff,
        }, returncode=returncode)

    def snapshot(self, packet):
        return {Path(packet[field]): Path(packet[field]).read_bytes()
                for field in ("state_file", "receipt")}

    def assert_unchanged(self, snapshot):
        for path, contents in snapshot.items():
            self.assertEqual(path.read_bytes(), contents, str(path))

    def test_child_repeats_then_parent_resumes_with_its_own_review_count(self):
        parent = self.start("parent", check='test "$(cat counter.txt)" = 2', reviews=2,
                            resources=[{"purpose": "child result", "locator": str(self.receipt("child"))}])
        before = self.snapshot(parent)
        child = self.start("child", check='test "$(cat counter.txt)" = 2', reviews=2,
                           resources=[{"purpose": "return to parent", "locator": parent["receipt"]}])
        self.assertNotEqual(parent["state_file"], child["state_file"])
        self.assertNotEqual(parent["done_argv"][-1], child["done_argv"][-1])

        (self.workspace / "counter.txt").write_text("1\n")
        child = self.finish(child, assessment="unsatisfied", classification="trivial",
                            evidence="Counter is 1; a second child action must write 2.")
        self.assertEqual(child["status"], "active")
        self.assertEqual(child["progress"]["action_number"], 2)
        # Lost conversation context: each owner retrieves its own current packet.
        self.assertEqual(self.refresh("child"), child)
        (self.workspace / "counter.txt").write_text("2\n")
        self.verify(child)
        child_terminal = self.finish(child, classification="trivial")
        self.assertEqual(child_terminal["status"], "complete")
        self.assertEqual(child_terminal["progress"]["trivial_streak"], 2)
        self.assertFalse(Path(child["state_file"]).exists())
        self.assertEqual(self.saved("child"), child_terminal)
        self.assert_unchanged(before)

        self.assertEqual(self.refresh("parent"), parent)
        self.assertIsNone(parent["last_check_results"])
        refused = self.finish(parent, returncode=2)
        self.assertIn("no verify run", refused["error"])
        self.assert_unchanged(before)
        self.verify(parent)
        parent = self.finish(parent, classification="trivial",
                             handoff=f"Child complete at {child_terminal['receipt']}; parent review remains.")
        self.assertEqual(parent["status"], "active")
        self.assertEqual(parent["progress"]["trivial_streak"], 1)
        self.assertEqual(parent["progress"]["action_number"], 2)
        self.assertEqual(parent["context"]["resources"][0]["locator"], str(self.receipt("child")))
        self.verify(parent)
        self.assertEqual(self.finish(parent, classification="trivial")["status"], "complete")
        self.assertEqual(self.saved("child"), child_terminal)
        self.assertEqual(list(self.states.iterdir()), [])

    def test_three_levels_unwind_from_a_relocated_package(self):
        package = self.root / "relocated package"
        shutil.copytree(ROOT / "plugins/until-loop", package)
        self.script = package / "skills/until-loop/scripts/until_loop_ephemeral.py"
        parent = self.start("parent")
        child = self.start("child")
        grandchild = self.start("grandchild")
        packets = [parent, child, grandchild]
        self.assertEqual(len({p["state_file"] for p in packets}), 3)
        for index in (2, 1, 0):
            suspended = [self.snapshot(p) for p in packets[:index]]
            name = ("parent", "child", "grandchild")[index]
            packet = self.refresh(name)
            self.assertEqual(Path(packet["done_argv"][1]), self.script)
            (self.workspace / (name + ".txt")).write_text("ready\n")
            self.verify(packet)
            terminal = self.finish(packet)
            self.assertEqual(terminal["status"], "complete")
            self.assertEqual(self.saved(name), terminal)
            self.assertFalse(Path(packet["state_file"]).exists())
            for snapshot in suspended:
                self.assert_unchanged(snapshot)
        self.assertEqual(list(self.states.iterdir()), [])

    def test_incomplete_child_does_not_finish_or_advance_parent(self):
        for reason in ("blocked", "cancelled"):
            with self.subTest(reason=reason):
                parent_name, child_name = "parent_" + reason, "child_" + reason
                parent = self.start(parent_name)
                before = self.snapshot(parent)
                child = self.start(child_name)
                child = self.finish(child, assessment="unknown", continuation=reason,
                                    classification="unresolved", evidence=f"Required child work {reason}.")
                self.assertEqual(child["status"], "stopped")
                self.assertFalse(Path(child["state_file"]).exists())
                self.assert_unchanged(before)
                self.assertEqual(self.refresh(parent_name), parent)
                # Host policy explicitly propagates the unmet dependency; the child cannot do this itself.
                stopped = self.finish(parent, assessment="unknown", continuation=reason,
                                      classification="unresolved",
                                      evidence=f"Required child stopped: {reason}.",
                                      handoff=f"Unmet child dependency: {child['receipt']}")
                self.assertEqual(stopped["status"], "stopped")
                self.assertIn(child["receipt"], stopped["last_report"]["handoff"])
                self.assertEqual(self.saved(child_name), child)
        self.assertEqual(list(self.states.iterdir()), [])

    def test_crossed_callbacks_and_check_recorders_leave_both_runs_unchanged(self):
        parent, child = self.start("parent"), self.start("child")
        snapshots = [self.snapshot(parent), self.snapshot(child)]
        for owner, other in ((parent, child), (child, parent)):
            with self.subTest(owner=owner["receipt"]):
                crossed = {**owner, "done_argv": [*owner["done_argv"][:-1], other["done_argv"][-1]]}
                error = self.finish(crossed, returncode=2)
                self.assertIn("action token does not match", error["error"])
                recorded = self.command(
                    [sys.executable, self.script, "record", "--state", owner["state_file"],
                     other["done_argv"][-1]], stdin="", returncode=2,
                )
                self.assertIn("UNTIL-LOOP-RECORD-ERROR", recorded.stdout)
                for snapshot in snapshots:
                    self.assert_unchanged(snapshot)

    def test_stopped_child_allows_parent_continuation_but_cannot_supply_success(self):
        parent = self.start("parent", check="test -f child.txt", reviews=2)
        child = self.start("child")
        child = self.finish(child, assessment="unknown", continuation="cancelled",
                            classification="unresolved", evidence="Only the child request was cancelled.")
        self.assertEqual(child["status"], "stopped")
        parent = self.finish(parent, assessment="unknown", classification="unresolved",
                             evidence="Child stopped; an authorized alternative remains.",
                             handoff=f"Missing child.txt; retain cancelled result at {child['receipt']}.")
        self.assertEqual(parent["status"], "active")
        self.assertEqual(parent["progress"]["action_number"], 2)
        self.assertEqual(parent["progress"]["trivial_streak"], 0)
        self.verify(parent, passed=False)
        # Even an incorrect success claim cannot turn absent child output into completion.
        parent = self.finish(parent, classification="trivial")
        self.assertEqual(parent["status"], "active")
        self.assertEqual(parent["progress"]["trivial_streak"], 0)
        self.assertFalse(parent["last_check_results"][0]["passed"])
        self.assertIn("artifact", parent["instruction"])
        self.assertEqual(self.saved("child"), child)

    def test_child_edit_invalidates_an_earlier_parent_check(self):
        self.command(["git", "-C", self.workspace, "init", "-q"])
        (self.workspace / "parent.txt").write_text("ready\n")
        parent = self.start("parent")
        self.verify(parent)
        before = self.snapshot(parent)
        child = self.start("child")
        (self.workspace / "child.txt").write_text("child changed the shared workspace\n")
        self.verify(child)
        self.assertEqual(self.finish(child)["status"], "complete")
        error = self.finish(parent, returncode=2)
        self.assertIn("workspace changed", error["error"])
        self.assert_unchanged(before)
        self.verify(parent)
        self.assertEqual(self.finish(parent)["status"], "complete")

    def test_child_cannot_reuse_parent_receipt_or_state_path(self):
        parent = self.start("parent")
        before = self.snapshot(parent)
        for occupied in (parent["receipt"], parent["state_file"]):
            with self.subTest(occupied=occupied):
                error = self.start("child", receipt=occupied, returncode=2)
                self.assertIn("receipt already exists", error["error"])
                self.assert_unchanged(before)
                self.assertEqual(list(self.states.iterdir()), [Path(parent["state_file"])])

    def test_unrelated_or_damaged_files_cannot_be_claimed_as_receipts(self):
        parent = self.start("parent")
        before = self.snapshot(parent)
        occupied = self.root / "unrelated.json"
        samples = [(self.root / "parent-contract.json").read_bytes(),
                   b"not JSON", b'{"status":"complete"}']
        for content in samples:
            with self.subTest(content=content):
                occupied.write_bytes(content)
                error = self.start("child", receipt=str(occupied), returncode=2)
                self.assertIn("receipt already exists", error["error"])
                self.assertEqual(occupied.read_bytes(), content)
                self.assert_unchanged(before)
                self.assertEqual(list(self.states.iterdir()), [Path(parent["state_file"])])

    def test_owner_can_explicitly_start_again_on_a_terminal_receipt(self):
        for continuation in ("allowed", "blocked"):
            with self.subTest(continuation=continuation):
                name = "child_" + continuation
                child = self.start(name)
                if continuation == "allowed":
                    (self.workspace / (name + ".txt")).write_text("ready\n")
                    self.verify(child)
                    terminal = self.finish(child)
                    self.assertEqual(terminal["status"], "complete")
                else:
                    terminal = self.finish(child, assessment="unknown", continuation="blocked",
                                           classification="unresolved")
                    self.assertEqual(terminal["status"], "stopped")
                # ShipLoop explicitly starts a later run on the same terminal receipt.
                restarted = self.start(name)
                self.assertEqual(restarted["status"], "active")
                self.assertNotEqual(restarted["state_file"], terminal["state_file"])
                self.assertEqual(restarted["progress"]["action_number"], 1)
                self.assertEqual(self.saved(name), restarted)
                self.assertFalse(Path(terminal["state_file"]).exists())
                self.finish(restarted, assessment="unknown", continuation="cancelled",
                            classification="unresolved")
        self.assertEqual(list(self.states.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
