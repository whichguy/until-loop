"""Exercise host SessionStart recovery without changing callback behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts" / "until_loop_ephemeral.py"
RECOVERY = ROOT / "scripts" / "until_loop_recover.py"
IDENTITY_ENV = (
    "UNTIL_LOOP_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GROK_SESSION_ID",
)
PACKET_MARKER = "Until Loop recovery packet:\n"


class RecoveryHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="until-loop-recovery-hook-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.state_dir = self.root / "session state"
        self.workspace.mkdir()
        self.state_dir.mkdir()

        self.base_environment = dict(os.environ)
        for name in IDENTITY_ENV:
            self.base_environment.pop(name, None)
        self.base_environment.pop("UNTIL_LOOP_STATE_DIR", None)
        self.base_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def environment(self, *, state_dir=None, **identity):
        result = dict(self.base_environment)
        result["UNTIL_LOOP_STATE_DIR"] = str(self.state_dir if state_dir is None else state_dir)
        result.update(identity)
        return result

    def run_process(self, argv, *, payload=None, raw_input=None, environment=None, cwd=None):
        input_bytes = raw_input
        if payload is not None:
            input_bytes = json.dumps(payload).encode("utf-8")
        result = subprocess.run(
            [str(value) for value in argv],
            input=input_bytes,
            capture_output=True,
            cwd=self.root if cwd is None else cwd,
            env=self.environment() if environment is None else environment,
            timeout=30,
            check=False,
        )
        return result

    def callback(self, argv, payload=None, *, environment, returncode=0):
        result = self.run_process(argv, payload=payload, environment=environment)
        self.assertEqual(
            result.returncode,
            returncode,
            result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"),
        )
        return result

    def contract(self, name, *, request=None, exit_condition=None, repeat_condition=None,
                 scope=None, authority=None):
        return {
            "workspace": str(self.workspace),
            "work": f"Complete the assigned {name} work and check its artifact.",
            "exit_condition": (exit_condition if exit_condition is not None
                               else f"The required evidence for {name} is current."),
            "repeat_condition": (repeat_condition if repeat_condition is not None
                                 else "Continue while authorized work remains and stop on an unmet dependency."),
            "required_trivial_reviews": 0,
            "context": {
                "request": request or f"Complete {name} and verify its result.",
                "scope": scope if scope is not None else f"Only the temporary workspace and {name} artifact.",
                "authority": authority if authority is not None else "Local fixture writes and checks only.",
                "environment": "Use the emitted Until Loop callbacks.",
                "resources": [],
            },
            "exit_checks": [],
        }

    @staticmethod
    def native_identity(host, session_id):
        if host == "codex":
            return {"CODEX_THREAD_ID": session_id}
        if host == "claude":
            return {"CLAUDE_CODE_SESSION_ID": session_id}
        raise AssertionError(f"unsupported test host: {host}")

    def start(self, name, *, host="codex", session_id, request=None, exit_condition=None,
              repeat_condition=None, scope=None, authority=None):
        result = self.callback(
            [sys.executable, RUNTIME, "start"],
            self.contract(
                name,
                request=request,
                exit_condition=exit_condition,
                repeat_condition=repeat_condition,
                scope=scope,
                authority=authority,
            ),
            environment=self.environment(**self.native_identity(host, session_id)),
        )
        packet = json.loads(result.stdout)
        self.assertEqual(packet["status"], "active")
        return packet

    def recover(self, host, event, *, environment=None):
        return self.run_process(
            [sys.executable, RECOVERY, "--host", host],
            payload=event,
            environment=self.environment() if environment is None else environment,
        )

    def report(self):
        return {
            "classification": "non-trivial",
            "exit_assessment": "satisfied",
            "continuation_assessment": "allowed",
            "evidence": "The assigned artifact was written and checked.",
            "handoff": "The artifact and the run receipt retain the result.",
        }

    def finish(self, packet, *, host="codex", session_id):
        return self.callback(
            packet["done_argv"],
            self.report(),
            environment=self.environment(**self.native_identity(host, session_id)),
        )

    def checkpoint(self, packet, handoff, *, session_id):
        result = self.callback(
            [
                sys.executable,
                RUNTIME,
                "checkpoint",
                "--state",
                packet["state_file"],
                packet["done_argv"][-1],
            ],
            {"handoff": handoff},
            environment=self.environment(CODEX_THREAD_ID=session_id),
        )
        return json.loads(result.stdout)

    def successful_packet(self, result, *, host=None, session_id=None):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(result.stdout, "successful recovery should emit hook context JSON")
        response = json.loads(result.stdout)
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "SessionStart")
        context = hook_output["additionalContext"]
        marker = context.find(PACKET_MARKER)
        if marker >= 0:
            self.assertGreater(marker, 0, "a static context prefix must precede the recovery packet")
            packet_text = context[marker + len(PACKET_MARKER):].strip()
            return response, context, json.loads(packet_text)

        self.assertIsNotNone(host, "bounded recovery output needs its explicit host identity to fetch")
        argv = self.fallback_argv(context)
        complete = self.run_process(
            argv,
            environment=self.environment(**self.native_identity(host, session_id)),
        )
        self.assertEqual(complete.returncode, 0, complete.stderr.decode(errors="replace"))
        return response, context, json.loads(complete.stdout)

    def assert_visible_failure(self, result, *, host):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(result.stdout, "recognized recovery failures should emit hook JSON")
        response = json.loads(result.stdout)
        visible = response["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(visible.strip(), "the recovery failure must be visible to the model")
        self.assertIn("recover", visible.casefold())
        if host == "codex":
            self.assertIs(response.get("continue"), False)
            self.assertTrue(response.get("stopReason"))
        else:
            self.assertNotIn("continue", response)
        return response

    def fallback_argv(self, context):
        prompt = (
            "execute this exact read-only argv to retrieve the COMPLETE current packet; "
            "do not start a new loop or use callbacks from the summary:\n"
        )
        self.assertIn(prompt, context)
        encoded = context.split(prompt, 1)[1].split("\nRead the returned", 1)[0]
        argv = json.loads(encoded)
        self.assertIsInstance(argv, list)
        self.assertTrue(argv)
        return argv

    def test_compact_recovers_current_child_read_only_then_unwinds_to_parent(self):
        session_id = "nested-codex-session"
        root = self.start(
            "A",
            session_id=session_id,
            scope="Only the temporary workspace and the A fixture.",
            authority='A may write "a.txt" locally; no external actions.',
            repeat_condition='Continue A checks locally; stop if "production credentials" are needed.',
        )
        parent = self.start(
            "B",
            session_id=session_id,
            exit_condition='B exports "résumé.csv" and verifies its local rows.',
            repeat_condition='Continue while B fixtures are writable; stop on a lost schema owner — café.',
            scope="Only the temporary workspace and the B fixture.",
            authority='B may update "b.txt" locally; no external actions.',
        )
        checkpoint = "The B frame owns this saved partial-action note."
        self.checkpoint(parent, checkpoint, session_id=session_id)
        child_conditions = {
            "exit": 'C writes "猫.csv" or JSON with a schema, then verifies the chosen format.',
            "repeat": 'Continue while C can use local fixtures; stop if the sandbox is unavailable — naïve.',
        }
        self.start(
            "C",
            session_id=session_id,
            exit_condition=child_conditions["exit"],
            repeat_condition=child_conditions["repeat"],
        )
        state_file = next(self.state_dir.glob("innerloop-session-*.json"))
        lock_file = state_file.with_suffix(state_file.suffix + ".lock")
        self.assertTrue(lock_file.exists())
        lock_file.unlink()
        files_before = {path.name for path in self.state_dir.iterdir()}
        before = state_file.read_bytes()
        event = {"hook_event_name": "SessionStart", "source": "compact", "session_id": session_id}

        first = self.recover("codex", event)
        _, _, first_packet = self.successful_packet(first)
        repeated = self.recover("codex", event)
        _, _, repeated_packet = self.successful_packet(repeated)

        self.assertEqual(first.stdout, repeated.stdout)
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual({path.name for path in self.state_dir.iterdir()}, files_before)
        self.assertFalse(lock_file.exists(), "read_snapshot must not recreate the writer lock")
        self.assertEqual(first_packet["work"], self.contract("C")["work"])
        self.assertEqual(first_packet["stack"]["depth"], 3)
        self.assertEqual(first_packet["conditions"], child_conditions)
        self.assertEqual(first_packet["stack"]["ancestor_limits"], [
            {"frame_id": root["stack"]["frames"][0]["frame_id"],
             "scope": root["context"]["scope"], "authority": root["context"]["authority"],
             "repeat_condition": root["conditions"]["repeat"]},
            {"frame_id": parent["stack"]["frames"][1]["frame_id"],
             "scope": parent["context"]["scope"], "authority": parent["context"]["authority"],
             "repeat_condition": parent["conditions"]["repeat"]},
        ])
        self.assertEqual(first_packet["done_argv"], repeated_packet["done_argv"])
        self.assertEqual(first_packet["progress"]["action_number"], 1)
        self.assertEqual(
            first_packet["progress"]["action_number"],
            repeated_packet["progress"]["action_number"],
        )

        self.finish(first_packet, session_id=session_id)
        after_child_done = self.recover("codex", event)
        _, _, parent_packet = self.successful_packet(after_child_done)
        self.assertEqual(parent_packet["work"], self.contract("B")["work"])
        self.assertEqual(parent_packet["stack"]["depth"], 2)
        self.assertEqual(parent_packet["stack"]["checkpoint"], checkpoint)
        self.assertEqual(parent_packet["conditions"], parent["conditions"])
        self.assertEqual(parent_packet["stack"]["ancestor_limits"], [
            {"frame_id": root["stack"]["frames"][0]["frame_id"],
             "scope": root["context"]["scope"], "authority": root["context"]["authority"],
             "repeat_condition": root["conditions"]["repeat"]},
        ])
        self.assertEqual(parent_packet["stack"]["child_results"][-1]["packet"]["conditions"],
                         child_conditions)

    def test_payload_session_id_overrides_conflicting_inherited_codex_thread_id(self):
        target_id = "payload-thread"
        inherited_id = "launching-thread"
        target = self.start("payload-selected session", session_id=target_id)
        self.start("inherited session", session_id=inherited_id)

        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": target_id},
            environment=self.environment(CODEX_THREAD_ID=inherited_id),
        )

        _, _, packet = self.successful_packet(result)
        self.assertEqual(packet["work"], target["work"])

    def test_payload_id_preserves_other_native_identity_components(self):
        target_id = "mixed-payload-thread"
        other_host_ids = {
            "CLAUDE_CODE_SESSION_ID": "outer-claude-session",
            "GROK_SESSION_ID": "outer-grok-session",
        }
        # Ordinary callbacks compose every native host identity present in the environment.
        result = self.callback(
            [sys.executable, RUNTIME, "start"],
            self.contract("mixed identity target"),
            environment=self.environment(CODEX_THREAD_ID=target_id, **other_host_ids),
        )
        composite_target = json.loads(result.stdout)
        self.assertEqual(composite_target["status"], "active")

        matching = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": target_id},
            environment=self.environment(CODEX_THREAD_ID="launcher-thread", **other_host_ids),
        )
        _, _, packet = self.successful_packet(matching)
        self.assertEqual(packet["work"], composite_target["work"])

        mismatched = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": target_id},
            environment=self.environment(
                CODEX_THREAD_ID="launcher-thread",
                CLAUDE_CODE_SESSION_ID="different-outer-claude",
                GROK_SESSION_ID=other_host_ids["GROK_SESSION_ID"],
            ),
        )
        self.assertEqual(mismatched.returncode, 0, mismatched.stderr.decode(errors="replace"))
        self.assertEqual(mismatched.stdout, b"")

    def test_new_payload_session_is_silent_even_when_inherited_session_is_active(self):
        active_id = "active-launching-thread"
        self.start("existing active session", session_id=active_id)

        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": "new-session"},
            environment=self.environment(CODEX_THREAD_ID=active_id),
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")

    def test_missing_state_is_silent_and_does_not_create_storage_directory(self):
        missing_state_dir = self.root / "missing session storage"
        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": "absent-session"},
            environment=self.environment(state_dir=missing_state_dir),
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertFalse(missing_state_dir.exists())

    def test_terminal_state_is_reported_without_a_done_callback(self):
        session_id = "terminal-session"
        active = self.start("terminal root", session_id=session_id)
        terminal = json.loads(self.finish(active, session_id=session_id).stdout)
        self.assertEqual(terminal["status"], "complete")
        state_file = Path(active["state_file"])
        before = state_file.read_bytes()

        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": session_id},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        response = json.loads(result.stdout)
        notice = response["hookSpecificOutput"]["additionalContext"]

        self.assertIn("already ended (complete)", notice)
        self.assertNotIn(PACKET_MARKER, notice)
        self.assertEqual(state_file.read_bytes(), before)

    def test_corrupt_and_mismatched_session_state_fail_visibly_without_rewrite(self):
        for case in ("corrupt", "mismatched-key"):
            with self.subTest(case=case):
                session_id = f"damaged-{case}"
                active = self.start(case, session_id=session_id)
                state_file = Path(active["state_file"])
                if case == "corrupt":
                    state_file.write_bytes(b"{broken json")
                else:
                    document = json.loads(state_file.read_bytes())
                    document["session_key"] = "f" * 64
                    state_file.write_text(json.dumps(document), encoding="utf-8")
                before = state_file.read_bytes()

                result = self.recover(
                    "codex",
                    {"hook_event_name": "SessionStart", "source": "compact", "session_id": session_id},
                )

                self.assert_visible_failure(result, host="codex")
                self.assertEqual(state_file.read_bytes(), before)

    def test_recognized_malformed_session_start_fails_visibly_without_storage(self):
        missing_state_dir = self.root / "malformed event storage"
        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "resume"},
            environment=self.environment(state_dir=missing_state_dir),
        )

        self.assert_visible_failure(result, host="codex")
        self.assertFalse(missing_state_dir.exists())

    def test_unrelated_event_clear_and_fork_are_ignored_without_state_changes(self):
        session_id = "ignored-events-session"
        self.start("active session", session_id=session_id)
        state_file = next(self.state_dir.glob("innerloop-session-*.json"))
        before = state_file.read_bytes()
        ignored_events = (
            {"hook_event_name": "PreToolUse", "source": "compact", "session_id": session_id},
            {"hook_event_name": "SessionStart", "source": "clear", "session_id": session_id},
        )

        for event in ignored_events:
            with self.subTest(event=event):
                result = self.recover("codex", event)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout, b"")
                self.assertEqual(state_file.read_bytes(), before)

    def test_fork_emits_warning_about_copied_callback_handles(self):
        result = self.recover("codex", {"hook_event_name": "SessionStart", "source": "fork"})

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        response = json.loads(result.stdout)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new fork", context)
        self.assertIn("callbacks", context)
        self.assertIn("original conversation", context)

    def test_explicit_session_override_skips_automatic_recovery(self):
        session_id = "explicit-skip-session"
        active = self.start("active host session", session_id=session_id)
        state_file = Path(active["state_file"])
        before = state_file.read_bytes()
        lock_file = state_file.with_suffix(state_file.suffix + ".lock")
        lock_file.unlink()

        result = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": session_id},
            environment=self.environment(
                CODEX_THREAD_ID=session_id,
                UNTIL_LOOP_SESSION_ID="adapter-owned-namespace",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(state_file.read_bytes(), before)
        self.assertFalse(lock_file.exists())

    def test_claude_missing_or_mismatched_native_identity_skips_recovery(self):
        session_id = "claude-session-id"
        self.start("Claude active work", host="claude", session_id=session_id)
        event = {"hook_event_name": "SessionStart", "source": "resume", "session_id": session_id}

        cases = (
            ("missing", self.environment()),
            ("mismatched", self.environment(CLAUDE_CODE_SESSION_ID="other-claude-session")),
        )
        for case, environment in cases:
            with self.subTest(case=case):
                result = self.recover("claude", event, environment=environment)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout, b"")

    def test_host_budget_fallback_argv_recovers_the_full_packet(self):
        cases = (("codex", 32_000, 36_000), ("claude", 9_500, 12_000))
        for host, byte_budget, request_length in cases:
            with self.subTest(host=host):
                session_id = f"large-{host}-session"
                request = "large-request-" + ("x" * request_length)
                self.start("large packet", host=host, session_id=session_id, request=request)
                result = self.recover(
                    host,
                    {"hook_event_name": "SessionStart", "source": "compact", "session_id": session_id},
                    environment=self.environment(**self.native_identity(host, session_id)),
                )

                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                response = json.loads(result.stdout)
                context = response["hookSpecificOutput"]["additionalContext"]
                self.assertNotIn(PACKET_MARKER, context)
                self.assertLessEqual(len(context.encode("utf-8")), byte_budget)
                argv = self.fallback_argv(context)
                self.assertIn(str(RUNTIME), argv[1])
                complete = self.run_process(
                    argv,
                    environment=self.environment(**self.native_identity(host, session_id)),
                )
                self.assertEqual(complete.returncode, 0, complete.stderr.decode(errors="replace"))
                packet = json.loads(complete.stdout)
                self.assertEqual(packet["context"]["request"], request)
                self.assertTrue(packet["done_argv"])

    def test_isolated_directory_run_is_not_recovered_as_a_session(self):
        session_id = "isolated-session"
        isolated_directory = self.root / "isolated callback state"
        isolated_directory.mkdir()
        result = self.callback(
            [sys.executable, RUNTIME, "start", "--directory", isolated_directory],
            self.contract("isolated run"),
            environment=self.environment(**self.native_identity("codex", session_id)),
        )
        isolated_packet = json.loads(result.stdout)
        self.assertTrue(Path(isolated_packet["state_file"]).exists())
        self.assertEqual(list(self.state_dir.glob("innerloop-session-*.json")), [])

        recovered = self.recover(
            "codex",
            {"hook_event_name": "SessionStart", "source": "startup", "session_id": session_id},
        )

        self.assertEqual(recovered.returncode, 0, recovered.stderr.decode(errors="replace"))
        self.assertEqual(recovered.stdout, b"")

    def test_claude_native_session_id_recovers_matching_state(self):
        session_id = "claude-native-session"
        parent_conditions = {
            "exit": 'The parent creates "café.csv" and confirms every local row.',
            "repeat": 'Continue while the parent fixture is local; stop if its owner is unclear — naïve.',
        }
        child_conditions = {
            "exit": 'The child creates JSON with a schema, or CSV without one, then checks the selected form.',
            "repeat": 'Continue while the child fixture is writable; stop if production access is needed — niño.',
        }
        parent = self.start(
            "Claude session work",
            host="claude",
            session_id=session_id,
            exit_condition=parent_conditions["exit"],
            repeat_condition=parent_conditions["repeat"],
        )
        child = self.start(
            "Claude child work",
            host="claude",
            session_id=session_id,
            exit_condition=child_conditions["exit"],
            repeat_condition=child_conditions["repeat"],
        )

        result = self.recover(
            "claude",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": session_id},
            environment=self.environment(**self.native_identity("claude", session_id)),
        )

        _, _, packet = self.successful_packet(result, host="claude", session_id=session_id)
        self.assertEqual(packet["work"], child["work"])
        self.assertEqual(packet["conditions"], child_conditions)
        self.assertEqual(packet["stack"]["ancestor_limits"], [{
            "frame_id": parent["stack"]["frames"][0]["frame_id"],
            "scope": parent["context"]["scope"],
            "authority": parent["context"]["authority"],
            "repeat_condition": parent_conditions["repeat"],
        }])

        self.finish(packet, host="claude", session_id=session_id)
        returned = self.recover(
            "claude",
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": session_id},
            environment=self.environment(**self.native_identity("claude", session_id)),
        )
        _, _, resumed_parent = self.successful_packet(
            returned, host="claude", session_id=session_id
        )
        self.assertEqual(resumed_parent["work"], parent["work"])
        self.assertEqual(resumed_parent["conditions"], parent_conditions)
        self.assertEqual(
            resumed_parent["stack"]["child_results"][-1]["packet"]["conditions"],
            child_conditions,
        )

    def test_claude_corrupt_state_failure_is_visible_without_codex_stop_field(self):
        session_id = "claude-corrupt-session"
        active = self.start("Claude corrupt state", host="claude", session_id=session_id)
        state_file = Path(active["state_file"])
        state_file.write_bytes(b"{broken json")
        before = state_file.read_bytes()

        result = self.recover(
            "claude",
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": session_id},
            environment=self.environment(**self.native_identity("claude", session_id)),
        )

        self.assert_visible_failure(result, host="claude")
        self.assertEqual(state_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
