"""Automatic, script-owned nesting through one session stack and the real CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "until_loop_ephemeral.py"


class SessionStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="until-loop-session-stack-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.state_dir = self.root / "session-state"
        self.other_cwd = self.root / "other-cwd"
        for directory in (self.workspace, self.state_dir, self.other_cwd):
            directory.mkdir()
        self.session_id = "session-" + self.root.name
        self.environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def command(self, argv, *, payload=None, session_id=None, cwd=None, returncode=0):
        environment = {
            **self.environment,
            "UNTIL_LOOP_SESSION_ID": session_id or self.session_id,
            "UNTIL_LOOP_STATE_DIR": str(self.state_dir),
        }
        result = subprocess.run(
            [str(arg) for arg in argv],
            input=None if payload is None else json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=self.root if cwd is None else cwd,
            env=environment,
            timeout=30,
        )
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        return result

    def invoke(self, argv, payload=None, **options):
        result = self.command(argv, payload=payload, **options)
        return json.loads(result.stdout)

    def contract(self, name, check=None):
        checks = [] if check is None else [
            {"id": "artifact", "clause": f"{name} artifact exists", "command": check}
        ]
        return {
            "workspace": str(self.workspace),
            "work": f"Complete one assigned {name} iteration and check its artifact.",
            "exit_condition": f"The required work for {name} has current artifact evidence.",
            "repeat_condition": "Continue while authorized work remains; stop on an unmet dependency.",
            "required_trivial_reviews": 0,
            "context": {
                "request": f"Complete {name} and verify its artifact.",
                "scope": f"Only the temporary workspace and {name}'s assigned artifact.",
                "authority": "Local fixture writes and checks only; no external actions.",
                "environment": "Use the emitted CLI callbacks and POSIX shell exit checks.",
                "resources": [],
            },
            "exit_checks": checks,
        }

    def start(self, name, *, check=None, session_id=None):
        packet = self.invoke(
            [sys.executable, SCRIPT, "start"],
            self.contract(name, check),
            session_id=session_id,
        )
        self.assertEqual(packet["status"], "active")
        return packet

    def next(self, *, session_id=None, cwd=None):
        return self.invoke(
            [sys.executable, SCRIPT, "next"], session_id=session_id, cwd=cwd
        )

    def finish(
        self,
        packet,
        *,
        assessment="satisfied",
        continuation="allowed",
        classification="non-trivial",
        evidence="The assigned artifact was written and checked.",
        handoff="The artifact and this run's receipt retain the result.",
        session_id=None,
        returncode=0,
    ):
        return self.invoke(
            packet["done_argv"],
            {
                "classification": classification,
                "exit_assessment": assessment,
                "continuation_assessment": continuation,
                "evidence": evidence,
                "handoff": handoff,
            },
            session_id=session_id,
            returncode=returncode,
        )

    def checkpoint(self, packet, handoff):
        return self.invoke(
            [
                sys.executable,
                SCRIPT,
                "checkpoint",
                "--state",
                packet["state_file"],
                packet["done_argv"][-1],
            ],
            {"handoff": handoff},
        )

    def verify(self, packet):
        return subprocess.run(
            ["/bin/sh", "-c", packet["verify_script"]],
            text=True,
            capture_output=True,
            cwd=self.root,
            env={
                **self.environment,
                "UNTIL_LOOP_SESSION_ID": self.session_id,
                "UNTIL_LOOP_STATE_DIR": str(self.state_dir),
            },
            timeout=30,
        )

    def test_two_level_child_repeats_then_returns_without_advancing_parent(self):
        parent = self.start("parent")
        child = self.start("child")
        self.assertEqual(parent["state_file"], child["state_file"])
        self.assertEqual(child["stack"]["depth"], 2)

        repeated = self.finish(child, assessment="unsatisfied")
        self.assertEqual(repeated["status"], "active")
        self.assertEqual(repeated["progress"]["action_number"], 2)
        self.assertEqual(repeated["stack"]["depth"], 2)

        resumed = self.finish(repeated)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["stack"]["depth"], 1)
        self.assertEqual(resumed["progress"]["action_number"], 1)
        child_result = resumed["stack"]["child_results"][-1]["packet"]
        self.assertEqual(child_result["status"], "complete")
        self.assertEqual(child_result["progress"]["action_number"], 2)

    def test_three_levels_unwind_in_lifo_order(self):
        parent = self.start("parent")
        child = self.start("child")
        grandchild = self.start("grandchild")
        self.assertEqual(parent["state_file"], child["state_file"])
        self.assertEqual(child["state_file"], grandchild["state_file"])
        self.assertEqual(grandchild["stack"]["depth"], 3)

        at_child = self.finish(grandchild)
        self.assertEqual(at_child["status"], "active")
        self.assertEqual(at_child["stack"]["depth"], 2)
        self.assertEqual(at_child["work"], child["work"])

        at_parent = self.finish(at_child)
        self.assertEqual(at_parent["status"], "active")
        self.assertEqual(at_parent["stack"]["depth"], 1)
        self.assertEqual(at_parent["work"], parent["work"])

    def test_three_level_recovery_needs_no_retained_packets(self):
        self.start("root")
        self.start("child")
        self.start("grandchild")
        # Only the stable session binding survives, not state paths or callbacks.
        for name, depth in (("grandchild", 3), ("child", 2), ("root", 1)):
            packet = self.next(cwd=self.other_cwd)
            self.assertEqual(packet["work"], self.contract(name)["work"])
            self.assertEqual(packet["stack"]["depth"], depth)
            result = self.finish(packet)
            self.assertEqual(result["status"], "complete" if depth == 1 else "active")
            del packet, result
        self.assertEqual(self.next()["status"], "complete")

    def test_parent_callbacks_and_check_record_are_stale_while_child_runs_and_after_return(self):
        parent = self.start("parent", check="test -f parent.ready")
        (self.workspace / "parent.ready").write_text("ready\n", encoding="utf-8")
        verified = self.verify(parent)
        self.assertIn("all passed", verified.stdout)

        child = self.start("child")
        during_child = self.verify(parent)
        self.assertIn("UNTIL-LOOP-RECORD-ERROR", during_child.stdout)
        refused = self.finish(parent, returncode=2)
        self.assertEqual(refused["status"], "error")
        self.assertIn("action token does not match", refused["error"])

        resumed = self.finish(child)
        self.assertEqual(resumed["progress"]["action_number"], 1)
        self.assertNotEqual(parent["done_argv"][-1], resumed["done_argv"][-1])
        after_return = self.verify(parent)
        self.assertIn("UNTIL-LOOP-RECORD-ERROR", after_return.stdout)
        stale = self.finish(parent, returncode=2)
        self.assertEqual(stale["status"], "error")
        self.assertIn("action token does not match", stale["error"])

        needs_verify = self.finish(resumed, returncode=2)
        self.assertEqual(needs_verify["status"], "error")
        self.assertIn("verify", needs_verify["error"])
        self.assertIn("all passed", self.verify(resumed).stdout)
        self.assertEqual(self.finish(resumed)["status"], "complete")

    def test_checkpoint_survives_cold_next_after_cwd_change(self):
        packet = self.start("parent")
        note = "Child is responsible for the generated fixture; resume at review."
        saved = self.checkpoint(packet, note)
        self.assertEqual(saved["progress"]["action_number"], 1)
        self.assertEqual(saved["stack"]["checkpoint"], note)
        del packet

        recovered = self.next(cwd=self.other_cwd)
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["progress"]["action_number"], 1)
        self.assertEqual(recovered["stack"]["depth"], 1)
        self.assertEqual(recovered["stack"]["checkpoint"], note)

    def test_stopped_child_and_parent_checkpoint_survive_cold_recovery(self):
        parent = self.start("parent")
        note = "Parent must review the stopped dependency before deciding."
        self.checkpoint(parent, note)
        child = self.start("child")
        resumed = self.finish(
            child,
            assessment="unknown",
            continuation="blocked",
            classification="unresolved",
            evidence="The child dependency remains blocked.",
        )
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["stack"]["depth"], 1)
        child_result = resumed["stack"]["child_results"][-1]["packet"]
        self.assertEqual(child_result["status"], "stopped")
        self.assertEqual(resumed["stack"]["checkpoint"], note)
        self.assertEqual(resumed["progress"]["action_number"], 1)

        recovered = self.next()
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["stack"]["checkpoint"], note)
        self.assertEqual(recovered["stack"]["child_results"], resumed["stack"]["child_results"])
        self.assertEqual(self.finish(recovered)["status"], "complete")

    def test_sessions_are_independent_in_the_same_state_directory(self):
        first = self.start("first session", session_id="session-one")
        second = self.start("second session", session_id="session-two")
        self.assertNotEqual(first["state_file"], second["state_file"])

        first_child = self.start("first child", session_id="session-one")
        self.assertEqual(first_child["stack"]["depth"], 2)
        self.assertEqual(self.next(session_id="session-two")["work"], second["work"])
        refused = self.finish(first_child, session_id="session-two", returncode=2)
        self.assertEqual(refused["status"], "error")
        self.assertEqual(refused["state_change"], "unchanged")
        self.assertEqual(self.next(session_id="session-one")["stack"]["depth"], 2)
        first_parent = self.finish(first_child, session_id="session-one")
        self.assertEqual(first_parent["work"], first["work"])
        self.assertEqual(self.next(session_id="session-two")["stack"]["depth"], 1)

    def test_root_terminal_replays_until_an_explicit_new_start(self):
        first = self.start("first root")
        terminal = self.finish(first)
        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(self.next(), terminal)

        second = self.start("second root")
        self.assertEqual(second["status"], "active")
        self.assertEqual(second["stack"]["depth"], 1)
        self.assertEqual(second["progress"]["action_number"], 1)
        self.assertNotEqual(first["done_argv"][-1], second["done_argv"][-1])


if __name__ == "__main__":
    unittest.main()
