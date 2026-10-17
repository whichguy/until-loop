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

    def contract(self, name, check=None, *, exit_condition=None, repeat_condition=None,
                 scope=None, authority=None):
        checks = [] if check is None else [
            {"id": "artifact", "clause": f"{name} artifact exists", "command": check}
        ]
        return {
            "workspace": str(self.workspace),
            "work": f"Complete one assigned {name} iteration and check its artifact.",
            "exit_condition": (exit_condition if exit_condition is not None
                               else f"The required work for {name} has current artifact evidence."),
            "repeat_condition": (repeat_condition if repeat_condition is not None
                                 else "Continue while authorized work remains; stop on an unmet dependency."),
            "required_trivial_reviews": 0,
            "context": {
                "request": f"Complete {name} and verify its artifact.",
                "scope": (scope if scope is not None
                          else f"Only the temporary workspace and {name}'s assigned artifact."),
                "authority": (authority if authority is not None
                              else "Local fixture writes and checks only; no external actions."),
                "environment": "Use the emitted CLI callbacks and POSIX shell exit checks.",
                "resources": [],
            },
            "exit_checks": checks,
        }

    def start(self, name, *, check=None, session_id=None, exit_condition=None,
              repeat_condition=None, scope=None, authority=None):
        packet = self.invoke(
            [sys.executable, SCRIPT, "start"],
            self.contract(
                name,
                check,
                exit_condition=exit_condition,
                repeat_condition=repeat_condition,
                scope=scope,
                authority=authority,
            ),
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
        parent_conditions = {
            "exit": 'The parent file "export.csv" round-trips all rows; café records remain intact.',
            "repeat": 'Continue while the parent check is local; stop if "production credentials" are needed.',
        }
        child_conditions = {
            "exit": 'The child emits "échantillon.csv" or JSON with its schema, then verifies the chosen form.',
            "repeat": 'Continue while child fixtures are writable; stop if the sandbox becomes unavailable — naïve.',
        }
        parent = self.start(
            "parent",
            exit_condition=parent_conditions["exit"],
            repeat_condition=parent_conditions["repeat"],
        )
        child = self.start(
            "child",
            exit_condition=child_conditions["exit"],
            repeat_condition=child_conditions["repeat"],
        )
        self.assertEqual(parent["state_file"], child["state_file"])
        self.assertEqual(child["stack"]["depth"], 2)
        self.assertEqual(child["conditions"], child_conditions)

        repeated = self.finish(child, assessment="unsatisfied")
        self.assertEqual(repeated["status"], "active")
        self.assertEqual(repeated["progress"]["action_number"], 2)
        self.assertEqual(repeated["stack"]["depth"], 2)
        self.assertEqual(repeated["conditions"], child_conditions)

        recovered = self.next(cwd=self.other_cwd)
        self.assertEqual(recovered["conditions"], child_conditions)
        self.assertEqual(recovered["stack"]["depth"], 2)

        resumed = self.finish(recovered)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["stack"]["depth"], 1)
        self.assertEqual(resumed["progress"]["action_number"], 1)
        child_result = resumed["stack"]["child_results"][-1]["packet"]
        self.assertEqual(child_result["status"], "complete")
        self.assertEqual(child_result["progress"]["action_number"], 2)
        self.assertEqual(child_result["conditions"], child_conditions)
        self.assertEqual(resumed["conditions"], parent_conditions)

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

    def test_three_level_recovery_derives_and_unwinds_ancestor_limits(self):
        root = self.start(
            "root",
            exit_condition='Root export "root.csv" round-trips each record; café stays local.',
            repeat_condition=(
                'Continue while root checks are local; stop if "production credentials" are needed; '
                "the five-check cap applies to this frame only."
            ),
            scope="Root-local: only workspace/root.csv; the five-check cap belongs to this frame — résumé.",
            authority='May write only "root.csv" in the temporary workspace; no production credentials.',
        )
        child = self.start(
            "child",
            exit_condition='Child output is CSV, or JSON with its schema; verify whichever form was chosen.',
            repeat_condition='Continue while either local fixture path is available; stop if access is lost — niño.',
            scope="Child may use any export fixture in the temporary workspace.",
            authority="Child may create and verify any local export fixture.",
        )
        grandchild = self.start(
            "grandchild",
            exit_condition='Grandchild writes "fixture.json" with a matching schema and verifies both.',
            repeat_condition='Continue while the fixture is writable; stop if its schema owner is unclear — naïve.',
            scope="Grandchild may use its assigned local JSON fixture.",
            authority="Grandchild may write and verify the assigned fixture only.",
        )

        root_id = root["stack"]["frames"][0]["frame_id"]
        child_id = child["stack"]["frames"][1]["frame_id"]
        expected_ancestors = [
            {
                "frame_id": root_id,
                "scope": root["context"]["scope"],
                "authority": root["context"]["authority"],
                "repeat_condition": root["conditions"]["repeat"],
            },
            {
                "frame_id": child_id,
                "scope": child["context"]["scope"],
                "authority": child["context"]["authority"],
                "repeat_condition": child["conditions"]["repeat"],
            },
        ]
        self.assertEqual(grandchild["stack"]["ancestor_limits"], expected_ancestors)
        self.assertTrue(all(set(item) == {"frame_id", "scope", "authority", "repeat_condition"}
                            for item in grandchild["stack"]["ancestor_limits"]))
        self.assertEqual(grandchild["stack"]["frames"], [
            {"frame_id": root_id, "work": root["work"], "action_number": 1},
            {"frame_id": child_id, "work": child["work"], "action_number": 1},
            {"frame_id": grandchild["stack"]["frames"][2]["frame_id"],
             "work": grandchild["work"], "action_number": 1},
        ])
        self.assertEqual(grandchild["conditions"], {
            "exit": 'Grandchild writes "fixture.json" with a matching schema and verifies both.',
            "repeat": 'Continue while the fixture is writable; stop if its schema owner is unclear — naïve.',
        })
        self.assertIn("applicable user stops", grandchild["instruction"])
        self.assertIn("as cancelled", grandchild["instruction"])
        self.assertNotIn("stack.ancestor_limits", root["instruction"])
        self.assertEqual(root["stack"]["ancestor_limits"], [])

        state_file = Path(grandchild["state_file"])
        before_recovery = state_file.read_bytes()
        document = json.loads(before_recovery)
        self.assertNotIn("ancestor_limits", json.dumps(document))
        recovered = self.next(cwd=self.other_cwd)
        self.assertEqual(state_file.read_bytes(), before_recovery)
        self.assertEqual(recovered["stack"]["ancestor_limits"], expected_ancestors)
        self.assertEqual(recovered["conditions"], grandchild["conditions"])

        at_child = self.finish(recovered)
        self.assertEqual(at_child["stack"]["ancestor_limits"], expected_ancestors[:1])
        self.assertEqual(at_child["conditions"], child["conditions"])
        grandchild_result = at_child["stack"]["child_results"][-1]
        self.assertEqual(set(grandchild_result), {"frame_id", "packet", "checkpoint", "child_results"})
        self.assertEqual(grandchild_result["packet"]["conditions"], grandchild["conditions"])
        self.assertEqual(
            set(grandchild_result["packet"]),
            {"status", "workspace", "work", "conditions", "progress", "context", "last_report",
             "exit_checks", "last_check_results", "check_repairs"},
        )
        self.assertNotIn("ancestor_limits", json.dumps(grandchild_result))

        at_root = self.finish(at_child)
        self.assertEqual(at_root["stack"]["ancestor_limits"], [])
        self.assertEqual(at_root["conditions"], root["conditions"])
        self.assertEqual(at_root["progress"]["action_number"], 1)
        child_result = at_root["stack"]["child_results"][-1]
        self.assertEqual(child_result["packet"]["conditions"], child["conditions"])
        self.assertEqual(child_result["child_results"][-1]["packet"]["conditions"], grandchild["conditions"])
        self.assertNotIn("ancestor_limits", json.dumps(child_result))

        terminal = self.finish(at_root)
        self.assertEqual(terminal["status"], "complete")
        self.assertNotIn("ancestor_limits", json.dumps(terminal))

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
