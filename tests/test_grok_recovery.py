"""Exercise the Grok native recovery hooks at their JSON process boundary."""
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
FALLBACK_PROMPT = (
    "execute this exact read-only argv to retrieve the COMPLETE current packet; "
    "do not start a new loop or use callbacks from the summary:\n"
)
EVENT_NAMES = {
    "SessionStart": "session_start",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "PreToolUse": "pre_tool_use",
    "Stop": "stop",
}


class GrokRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="until-loop-grok-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.state_dir = self.root / "session state"
        self.workspace.mkdir()
        self.state_dir.mkdir()

        self.base_environment = dict(os.environ)
        for name in IDENTITY_ENV:
            self.base_environment.pop(name, None)
        self.base_environment.pop("GROK_HOOK_EVENT", None)
        self.base_environment.pop("UNTIL_LOOP_STATE_DIR", None)
        self.base_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def environment(self, *, state_dir=None, **values):
        result = dict(self.base_environment)
        result["UNTIL_LOOP_STATE_DIR"] = str(self.state_dir if state_dir is None else state_dir)
        result.update(values)
        return result

    def run_process(self, argv, *, payload=None, environment=None, cwd=None):
        return subprocess.run(
            [str(value) for value in argv],
            input=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root if cwd is None else cwd,
            env=self.environment() if environment is None else environment,
            timeout=30,
            check=False,
        )

    def contract(self, name, *, request=None, exit_checks=None):
        return {
            "workspace": str(self.workspace),
            "work": f"Complete the assigned {name} work and check its artifact.",
            "exit_condition": f"The required evidence for {name} is current.",
            "repeat_condition": "Continue while authorized work remains and stop on an unmet dependency.",
            "required_trivial_reviews": 0,
            "context": {
                "request": request or f"Complete {name} and verify its result.",
                "scope": f"Only the temporary workspace and {name} artifact.",
                "authority": "Local fixture writes and checks only.",
                "environment": "Use the emitted Until Loop callbacks.",
                "resources": [],
            },
            "exit_checks": [] if exit_checks is None else exit_checks,
        }

    def start(self, name, *, session_id, request=None, exit_checks=None):
        result = self.run_process(
            [sys.executable, RUNTIME, "start"],
            payload=self.contract(name, request=request, exit_checks=exit_checks),
            environment=self.environment(GROK_SESSION_ID=session_id),
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"),
        )
        packet = json.loads(result.stdout)
        self.assertEqual(packet["status"], "active")
        return packet

    @staticmethod
    def event(name, *, session_id, timestamp="2026-10-17T18:00:00+00:00", **fields):
        event = {
            "hookEventName": EVENT_NAMES[name],
            "hook_event_name": name,
            "sessionId": session_id,
            "session_id": session_id,
            "timestamp": timestamp,
        }
        event.update(fields)
        return event

    def recover(self, event, *, environment=None):
        if environment is None:
            environment = self.environment(
                GROK_HOOK_EVENT=event.get("hookEventName", ""),
                GROK_SESSION_ID=event.get("sessionId", ""),
            )
        return self.run_process(
            [sys.executable, RECOVERY, "--host", "grok"],
            payload=event,
            environment=environment,
        )

    def arm(self, name, *, session_id, timestamp, source):
        event = self.event(name, session_id=session_id, timestamp=timestamp, source=source)
        result = self.recover(event)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        return event

    def delivery_event(self, name, *, session_id, timestamp="2026-10-17T18:00:01+00:00", **fields):
        return self.event(name, session_id=session_id, timestamp=timestamp, **fields)

    def read_packet(self, result, *, session_id):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(result.stdout, "a claimed recovery must return hook output")
        response = json.loads(result.stdout)
        hook_output = response["hookSpecificOutput"]
        context = hook_output.get("permissionDecisionReason", hook_output.get("additionalContext", ""))
        marker = context.find(PACKET_MARKER)
        if marker >= 0:
            return response, context, json.loads(context[marker + len(PACKET_MARKER):].strip())

        self.assertIn(FALLBACK_PROMPT, context)
        encoded = context.split(FALLBACK_PROMPT, 1)[1].split("\nRead the returned", 1)[0]
        argv = json.loads(encoded)
        self.assertIsInstance(argv, list)
        self.assertTrue(argv)
        complete = self.run_process(
            argv,
            environment=self.environment(GROK_SESSION_ID=session_id),
        )
        self.assertEqual(complete.returncode, 0, complete.stderr.decode(errors="replace"))
        return response, context, json.loads(complete.stdout)

    @staticmethod
    def report(*, continuation="allowed", exit_assessment="satisfied"):
        return {
            "classification": "non-trivial",
            "exit_assessment": exit_assessment,
            "continuation_assessment": continuation,
            "evidence": "The assigned artifact was written and checked.",
            "handoff": "The artifact and the run receipt retain the result.",
        }

    def complete(self, packet, *, session_id, report=None):
        return self.run_process(
            packet["done_argv"],
            payload=self.report() if report is None else report,
            environment=self.environment(GROK_SESSION_ID=session_id),
        )

    def test_compaction_denies_one_tool_with_current_top_packet_and_preserves_stack(self):
        session_id = "nested-grok-session"
        self.start("A", session_id=session_id)
        child = self.start("B", session_id=session_id)
        checkpoint_text = "B's saved partial-action context survives recovery."
        checkpointed = self.run_process(
            child["stack"]["checkpoint_argv"],
            payload={"handoff": checkpoint_text},
            environment=self.environment(GROK_SESSION_ID=session_id),
        )
        self.assertEqual(checkpointed.returncode, 0, checkpointed.stderr.decode(errors="replace"))
        child = json.loads(checkpointed.stdout)
        state_file = Path(child["state_file"])
        before = json.loads(state_file.read_bytes())
        documents_before = set(self.state_dir.glob("*.json"))

        self.arm("PreCompact", session_id=session_id, timestamp="2026-10-17T18:00:00Z", source="manual")
        after_arm = json.loads(state_file.read_bytes())
        self.assertIn("grok_recovery", after_arm)
        self.assertEqual(after_arm["frames"], before["frames"])
        self.assertEqual(after_arm["terminal"], before["terminal"])
        self.assertEqual(set(self.state_dir.glob("*.json")), documents_before)
        self.assertEqual(len(list(self.state_dir.glob("*.json"))), 1)

        delivery = self.delivery_event("PreToolUse", session_id=session_id)
        result = self.recover(delivery)
        response, context, packet = self.read_packet(result, session_id=session_id)

        hook_output = response["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertEqual(packet["work"], child["work"])
        self.assertEqual(packet["stack"]["depth"], 2)
        self.assertEqual(packet["stack"]["checkpoint"], checkpoint_text)
        self.assertEqual(packet["context"], child["context"])
        self.assertLessEqual(len(context.encode("utf-8")), 9_500)

        repeated = self.recover(delivery)
        self.assertEqual(repeated.returncode, 0, repeated.stderr.decode(errors="replace"))
        self.assertEqual(repeated.stdout, b"")

    def test_completed_child_compaction_recovers_parent_with_child_result(self):
        session_id = "grok-child-return-session"
        parent = self.start("A", session_id=session_id)
        self.start("B", session_id=session_id)

        self.arm("PostCompact", session_id=session_id, timestamp="2026-10-17T18:01:00Z", source="auto")
        first = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        _, _, child_packet = self.read_packet(first, session_id=session_id)
        self.assertEqual(child_packet["work"], "Complete the assigned B work and check its artifact.")
        self.assertEqual(child_packet["stack"]["depth"], 2)

        completed = self.complete(child_packet, session_id=session_id)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(completed.stdout)["status"], "active")

        self.arm("PreCompact", session_id=session_id, timestamp="2026-10-17T18:02:00Z", source="auto")
        returned = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        _, _, parent_packet = self.read_packet(returned, session_id=session_id)
        self.assertEqual(parent_packet["work"], parent["work"])
        self.assertEqual(parent_packet["stack"]["depth"], 1)
        self.assertEqual(len(parent_packet["stack"]["child_results"]), 1)
        child_result = parent_packet["stack"]["child_results"][0]
        self.assertEqual(child_result["packet"]["status"], "complete")
        self.assertEqual(child_result["packet"]["work"], child_packet["work"])

    def test_all_supported_start_and_compaction_events_arm_active_sessions(self):
        session_id = "grok-supported-events"
        active = self.start("supported events", session_id=session_id)
        cases = (
            ("SessionStart", "new"),
            ("SessionStart", "load"),
            ("PreCompact", "manual"),
            ("PreCompact", "auto"),
            ("PostCompact", "manual"),
            ("PostCompact", "auto"),
        )
        for index, (name, source) in enumerate(cases):
            with self.subTest(name=name, source=source):
                seconds = index * 2
                stamp = f"2026-10-17T18:10:{seconds:02d}Z"
                self.arm(name, session_id=session_id, timestamp=stamp, source=source)
                delivered = self.recover(
                    self.delivery_event(
                        "PreToolUse",
                        session_id=session_id,
                        timestamp=f"2026-10-17T18:10:{seconds + 1:02d}Z",
                    )
                )
                _, _, packet = self.read_packet(delivered, session_id=session_id)
                self.assertEqual(packet["work"], active["work"])

    def test_stop_end_turn_injects_one_recovery_context_without_tool_use(self):
        session_id = "grok-stop-session"
        active = self.start("stop recovery", session_id=session_id)
        self.arm("PostCompact", session_id=session_id, timestamp="2026-10-17T18:20:00Z", source="manual")

        stop = self.recover(
            self.delivery_event("Stop", session_id=session_id, reason="end_turn")
        )
        self.assertEqual(stop.returncode, 0, stop.stderr.decode(errors="replace"))
        response, context, packet = self.read_packet(stop, session_id=session_id)
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "Stop")
        self.assertLessEqual(len(context.encode("utf-8")), 9_500)
        self.assertEqual(packet["work"], active["work"])

        repeated = self.recover(
            self.delivery_event("Stop", session_id=session_id, reason="end_turn")
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr.decode(errors="replace"))
        self.assertEqual(repeated.stdout, b"")

    def test_non_end_turn_stop_cancelled_unrelated_and_subagent_events_do_not_consume_gate(self):
        session_id = "grok-ignored-events"
        self.start("ignored events", session_id=session_id)
        state_file = Path(next(self.state_dir.glob("*.json")))

        ignored_arm = self.event(
            "PreCompact",
            session_id=session_id,
            source="manual",
            subagentType="researcher",
        )
        ignored_arm_result = self.recover(ignored_arm)
        self.assertEqual(ignored_arm_result.stdout, b"")
        self.assertNotIn("grok_recovery", json.loads(state_file.read_bytes()))

        self.arm("PreCompact", session_id=session_id, timestamp="2026-10-17T18:30:00Z", source="manual")
        ignored = (
            self.event("Stop", session_id=session_id, reason="shutdown"),
            {
                "hookEventName": "stop_cancelled",
                "hook_event_name": "StopCancelled",
                "sessionId": session_id,
                "session_id": session_id,
                "timestamp": "2026-10-17T18:30:01Z",
            },
            self.event("PreToolUse", session_id=session_id, subagentType="worker"),
            {
                "hookEventName": "unrelated_event",
                "hook_event_name": "OtherEvent",
                "sessionId": session_id,
                "session_id": session_id,
                "timestamp": "2026-10-17T18:30:01Z",
            },
        )
        for event in ignored:
            with self.subTest(event=event.get("hook_event_name")):
                result = self.recover(event)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout, b"")

        delivered = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        _, _, packet = self.read_packet(delivered, session_id=session_id)
        self.assertEqual(packet["work"], "Complete the assigned ignored events work and check its artifact.")

    def test_missing_state_and_missing_or_wrong_native_hook_selector_are_quiet(self):
        session_id = "grok-quiet-guards"
        self.start("quiet guard", session_id=session_id)
        valid = self.event("PreCompact", session_id=session_id, source="manual")

        wrong_event_env = self.environment(
            GROK_SESSION_ID=session_id,
            GROK_HOOK_EVENT="session_start",
        )
        wrong_event = self.recover(valid, environment=wrong_event_env)
        self.assertEqual(wrong_event.returncode, 0, wrong_event.stderr.decode(errors="replace"))
        self.assertEqual(wrong_event.stdout, b"")

        missing_native_id = self.recover(
            valid,
            environment=self.environment(GROK_HOOK_EVENT=valid["hookEventName"]),
        )
        self.assertEqual(missing_native_id.returncode, 0, missing_native_id.stderr.decode(errors="replace"))
        self.assertEqual(missing_native_id.stdout, b"")

        wrong_payload_alias = dict(valid, hook_event_name="OtherEvent")
        mismatched_alias = self.recover(wrong_payload_alias)
        self.assertEqual(mismatched_alias.returncode, 0, mismatched_alias.stderr.decode(errors="replace"))
        self.assertEqual(mismatched_alias.stdout, b"")

        # The generated shared hooks file also runs in Claude. A Claude payload
        # has no Grok hookEventName, even if the process inherited a Grok ID.
        claude_event = {
            "hook_event_name": "PreCompact",
            "source": "manual",
            "session_id": session_id,
            "timestamp": "2026-10-17T18:40:00Z",
        }
        inherited_id = self.recover(
            claude_event,
            environment=self.environment(GROK_SESSION_ID=session_id),
        )
        self.assertEqual(inherited_id.returncode, 0, inherited_id.stderr.decode(errors="replace"))
        self.assertEqual(inherited_id.stdout, b"")

        active_file = Path(next(self.state_dir.glob("*.json")))
        before_override = active_file.read_bytes()
        explicit_override = self.recover(
            valid,
            environment=self.environment(
                GROK_HOOK_EVENT=valid["hookEventName"],
                GROK_SESSION_ID=session_id,
                UNTIL_LOOP_SESSION_ID="adapter-owned-namespace",
            ),
        )
        self.assertEqual(explicit_override.returncode, 0, explicit_override.stderr.decode(errors="replace"))
        self.assertEqual(explicit_override.stdout, b"")
        self.assertEqual(active_file.read_bytes(), before_override)

        missing_state_dir = self.root / "missing state"
        missing_session = self.event(
            "PreCompact", session_id="no-session-file", source="manual"
        )
        missing = self.recover(
            missing_session,
            environment=self.environment(
                state_dir=missing_state_dir,
                GROK_SESSION_ID="no-session-file",
                GROK_HOOK_EVENT=missing_session["hookEventName"],
            ),
        )
        self.assertEqual(missing.returncode, 0, missing.stderr.decode(errors="replace"))
        self.assertEqual(missing.stdout, b"")
        self.assertFalse(missing_state_dir.exists())

    def test_payload_native_identity_mismatch_is_visible_and_does_not_touch_other_sessions(self):
        target = self.start("target grok session", session_id="target-grok-id")
        other = self.start("other grok session", session_id="other-grok-id")
        target_file = Path(target["state_file"])
        other_file = Path(other["state_file"])
        target_before = target_file.read_bytes()
        other_before = other_file.read_bytes()

        mismatched_native = self.event(
            "PreCompact", session_id="target-grok-id", source="manual"
        )
        result = self.recover(
            mismatched_native,
            environment=self.environment(
                GROK_HOOK_EVENT=mismatched_native["hookEventName"],
                GROK_SESSION_ID="other-grok-id",
            ),
        )
        self.assertTrue(result.stdout, "a native/payload identity conflict must be visible")
        self.assertIn("identity", result.stdout.decode(errors="replace").casefold())
        self.assertEqual(target_file.read_bytes(), target_before)
        self.assertEqual(other_file.read_bytes(), other_before)

        conflicting_aliases = self.event(
            "PreCompact", session_id="target-grok-id", source="manual"
        )
        conflicting_aliases["session_id"] = "other-grok-id"
        result = self.recover(conflicting_aliases)
        self.assertTrue(result.stdout, "conflicting payload session aliases must be visible")
        self.assertIn("identity", result.stdout.decode(errors="replace").casefold())
        self.assertEqual(target_file.read_bytes(), target_before)
        self.assertEqual(other_file.read_bytes(), other_before)

    def test_recovery_gate_is_isolated_between_native_grok_sessions(self):
        target_id = "grok-isolation-target"
        other_id = "grok-isolation-other"
        target = self.start("target session work", session_id=target_id)
        self.start("other session work", session_id=other_id)
        self.arm("PostCompact", session_id=target_id, timestamp="2026-10-17T18:45:00Z", source="auto")

        other_delivery = self.recover(self.delivery_event("PreToolUse", session_id=other_id))
        self.assertEqual(other_delivery.returncode, 0, other_delivery.stderr.decode(errors="replace"))
        self.assertEqual(other_delivery.stdout, b"")

        target_delivery = self.recover(self.delivery_event("PreToolUse", session_id=target_id))
        _, _, packet = self.read_packet(target_delivery, session_id=target_id)
        self.assertEqual(packet["work"], target["work"])

    def test_timestamp_must_be_aware_iso_and_invalid_arming_does_not_rewrite_state(self):
        session_id = "grok-timestamp-session"
        active = self.start("timestamp validation", session_id=session_id)
        state_file = Path(active["state_file"])
        before = state_file.read_bytes()
        for timestamp in (None, "2026-10-17T18:00:00", "not-a-date"):
            with self.subTest(timestamp=timestamp):
                event = self.event(
                    "PreCompact", session_id=session_id, source="manual"
                )
                if timestamp is None:
                    event.pop("timestamp")
                else:
                    event["timestamp"] = timestamp
                result = self.recover(event)
                self.assertTrue(result.stdout, "invalid recognized timestamps must be visible")
                self.assertIn("timestamp", result.stdout.decode(errors="replace").casefold())
                self.assertEqual(state_file.read_bytes(), before)

    def test_duplicate_or_older_arm_after_delivery_stays_quiet_until_newer_compaction(self):
        session_id = "grok-rearm-session"
        active = self.start("timestamp ordered recovery", session_id=session_id)
        original = "2026-10-17T18:50:00Z"
        self.arm("SessionStart", session_id=session_id, timestamp=original, source="load")
        first = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        _, _, packet = self.read_packet(first, session_id=session_id)
        self.assertEqual(packet["work"], active["work"])

        self.arm("PreCompact", session_id=session_id, timestamp=original, source="manual")
        duplicate = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr.decode(errors="replace"))
        self.assertEqual(duplicate.stdout, b"")

        self.arm("PostCompact", session_id=session_id, timestamp="2026-10-17T18:49:59Z", source="auto")
        older = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        self.assertEqual(older.returncode, 0, older.stderr.decode(errors="replace"))
        self.assertEqual(older.stdout, b"")

        self.arm("PostCompact", session_id=session_id, timestamp="2026-10-17T18:50:01Z", source="auto")
        newer = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        _, _, newer_packet = self.read_packet(newer, session_id=session_id)
        self.assertEqual(newer_packet["work"], active["work"])

    def test_large_grok_packet_uses_bounded_context_and_actual_next_argv_fetch(self):
        session_id = "grok-large-packet"
        request = "Review this context carefully: " + ("猫" * 4_000)
        active = self.start("large packet", session_id=session_id, request=request)
        self.arm("PreCompact", session_id=session_id, timestamp="2026-10-17T18:55:00Z", source="manual")

        result = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        response = json.loads(result.stdout)
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "deny")
        reason = hook_output["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), 9_500)
        self.assertNotIn(PACKET_MARKER, reason)
        self.assertIn(FALLBACK_PROMPT, reason)

        _, _, fetched = self.read_packet(result, session_id=session_id)
        self.assertEqual(fetched["work"], active["work"])
        self.assertEqual(fetched["context"]["request"], request)

    def test_corrupt_state_is_preserved_and_hook_failures_are_host_safe(self):
        for hook_name in ("PreToolUse", "Stop"):
            with self.subTest(hook_name=hook_name):
                session_id = f"grok-corrupt-{hook_name}"
                active = self.start("corrupt state", session_id=session_id)
                state_file = Path(active["state_file"])
                state_file.write_bytes(b"{broken json")
                before = state_file.read_bytes()
                fields = {"reason": "end_turn"} if hook_name == "Stop" else {}
                result = self.recover(
                    self.delivery_event(hook_name, session_id=session_id, **fields)
                )
                response = json.loads(result.stdout)
                self.assertIn("recover", result.stdout.decode(errors="replace").casefold())
                if hook_name == "PreToolUse":
                    output = response["hookSpecificOutput"]
                    self.assertEqual(output["permissionDecision"], "deny")
                    self.assertTrue(output["permissionDecisionReason"].strip())
                else:
                    self.assertIs(response.get("continue"), False)
                self.assertEqual(state_file.read_bytes(), before)

    def test_cancelled_terminal_stack_stays_quiet_and_is_not_resumed(self):
        session_id = "grok-cancelled-stack"
        active = self.start("cancel this loop", session_id=session_id)
        cancelled = self.complete(
            active,
            session_id=session_id,
            report=self.report(continuation="cancelled", exit_assessment="unknown"),
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(cancelled.stdout)["status"], "stopped")

        state_file = Path(active["state_file"])
        before = json.loads(state_file.read_bytes())
        arm_after_terminal = self.recover(
            self.event(
                "PreCompact",
                session_id=session_id,
                timestamp="2026-10-17T18:59:00Z",
                source="auto",
            )
        )
        self.assertEqual(arm_after_terminal.returncode, 0, arm_after_terminal.stderr.decode(errors="replace"))
        self.assertEqual(arm_after_terminal.stdout, b"")
        self.assertNotIn("grok_recovery", json.loads(state_file.read_bytes()))
        self.assertEqual(before["terminal"]["status"], "stopped")

        delivery = self.recover(self.delivery_event("PreToolUse", session_id=session_id))
        self.assertEqual(delivery.returncode, 0, delivery.stderr.decode(errors="replace"))
        self.assertEqual(delivery.stdout, b"")

    def test_copied_mutation_callbacks_are_rejected_but_read_only_next_still_works(self):
        session_id = "grok-owned-callbacks"
        check = {"id": "artifact", "clause": "the artifact is checked", "command": "true"}
        packet = self.start("callback ownership", session_id=session_id, exit_checks=[check])
        state_file = Path(packet["state_file"])
        before = state_file.read_bytes()
        foreign_environment = self.environment(CODEX_THREAD_ID="forked-codex-session")

        callbacks = (
            ("checkpoint", packet["stack"]["checkpoint_argv"], {"handoff": "copied callback"}),
            ("done", packet["done_argv"], self.report()),
            ("record", ["/bin/sh", "-c", packet["verify_script"]], None),
        )
        for identity_case, environment in (
            ("different host", foreign_environment),
            ("missing host identity", self.environment()),
        ):
            for label, argv, payload in callbacks:
                with self.subTest(callback=label, identity=identity_case):
                    rejected = self.run_process(argv, payload=payload, environment=environment)
                    self.assertNotEqual(rejected.returncode, 0)
                    message = (rejected.stdout + rejected.stderr).decode(errors="replace").casefold()
                    self.assertTrue(
                        "session identity" in message or "stable host session id" in message,
                        message,
                    )
                    self.assertEqual(state_file.read_bytes(), before)

        readonly = self.run_process(
            [sys.executable, RUNTIME, "next", "--state", state_file],
            environment=foreign_environment,
        )
        self.assertEqual(readonly.returncode, 0, readonly.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(readonly.stdout)["work"], packet["work"])
        self.assertEqual(state_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
