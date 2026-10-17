"""Persistence, identity isolation, and crash-boundary checks for session stacks."""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "until_loop_ephemeral.py"
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import until_loop_ephemeral as runtime
import until_loop_session as sessions


_IDENTITY_ENV = (
    "UNTIL_LOOP_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GROK_SESSION_ID",
)


class SessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="until-loop-session-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.state_dir = self.root / "session-state"
        self.workspace.mkdir()
        self.state_dir.mkdir()

    def environment(self, *, session_id="storage-session", **identity):
        result = dict(os.environ)
        for name in _IDENTITY_ENV:
            result.pop(name, None)
        result["UNTIL_LOOP_STATE_DIR"] = str(self.state_dir)
        if session_id is not None:
            result["UNTIL_LOOP_SESSION_ID"] = session_id
        result.update(identity)
        return result

    def command(self, argv, *, payload=None, environment=None, cwd=None, returncode=0):
        result = subprocess.run(
            [str(arg) for arg in argv],
            input=None if payload is None else json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=self.root if cwd is None else cwd,
            env=self.environment() if environment is None else environment,
            timeout=30,
        )
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        return result

    def invoke(self, argv, payload=None, **options):
        result = self.command(argv, payload=payload, **options)
        return json.loads(result.stdout)

    def contract(self, name, *, workspace=None, check=None):
        checks = [] if check is None else [
            {"id": "artifact", "clause": f"{name} artifact exists", "command": check}
        ]
        return {
            "workspace": str(self.workspace if workspace is None else workspace),
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

    @staticmethod
    def report(*, assessment="satisfied", continuation="allowed", classification="non-trivial"):
        return {
            "classification": classification,
            "exit_assessment": assessment,
            "continuation_assessment": continuation,
            "evidence": "The assigned artifact was written and checked.",
            "handoff": "The artifact and this run's receipt retain the result.",
        }

    def locate(self, session_id="storage-session"):
        with mock.patch.dict(os.environ, self.environment(session_id=session_id), clear=True):
            return sessions.location(runtime)

    def direct_start(self, name, *, path=None, key=None, workspace=None, check=None):
        if path is None or key is None:
            path, key = self.locate()
        packet = sessions.start(runtime, path, key, self.contract(name, workspace=workspace, check=check))
        return path, key, packet

    @staticmethod
    def token(packet):
        return packet["done_argv"][-1].split("=", 1)[1]

    def test_missing_session_identity_fails_without_creating_state(self):
        environment = self.environment(session_id=None)
        result = self.command(
            [sys.executable, SCRIPT, "start"],
            payload=self.contract("root"),
            environment=environment,
            returncode=2,
        )
        error = json.loads(result.stdout)
        self.assertEqual(error["status"], "error")
        self.assertIn("stable host session ID", error["error"])
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_host_identity_composition_and_explicit_override(self):
        outer = self.locate_for_identities(CODEX_THREAD_ID="outer-thread")[0]
        composed = self.locate_for_identities(
            CODEX_THREAD_ID="outer-thread", CLAUDE_CODE_SESSION_ID="inner-session"
        )[0]
        explicit_with_hosts = self.locate_for_identities(
            UNTIL_LOOP_SESSION_ID="explicit-session",
            CODEX_THREAD_ID="outer-thread",
            CLAUDE_CODE_SESSION_ID="inner-session",
        )[0]
        explicit_alone = self.locate_for_identities(
            UNTIL_LOOP_SESSION_ID="explicit-session"
        )[0]
        self.assertNotEqual(outer, composed)
        self.assertEqual(explicit_with_hosts, explicit_alone)

    def locate_for_identities(self, **identity):
        environment = self.environment(session_id=None, **identity)
        with mock.patch.dict(os.environ, environment, clear=True):
            return sessions.location(runtime)

    def test_legacy_receipt_start_does_not_change_live_session_and_deletes_terminal_state(self):
        live = self.invoke(
            [sys.executable, SCRIPT, "start"],
            self.contract("live root"),
            environment=self.environment(session_id="live-session"),
        )
        live_path = Path(live["state_file"])
        live_bytes = live_path.read_bytes()

        receipt = self.root / "legacy-receipt.json"
        legacy = self.invoke(
            [sys.executable, SCRIPT, "start", "--receipt", receipt],
            self.contract("legacy isolated"),
            environment=self.environment(session_id="live-session"),
        )
        legacy_path = Path(legacy["state_file"])
        self.assertNotIn("stack", legacy)
        self.assertNotEqual(legacy_path, live_path)
        self.assertEqual(live_path.read_bytes(), live_bytes)

        terminal = self.invoke(legacy["done_argv"], self.report())
        self.assertEqual(terminal["status"], "complete")
        self.assertFalse(legacy_path.exists())
        self.assertTrue(receipt.exists())
        self.assertEqual(live_path.read_bytes(), live_bytes)

    def test_exit_check_bare_start_cannot_inherit_or_nest_live_session(self):
        child_contract = self.root / "bare-start-contract.json"
        child_contract.write_text(json.dumps(self.contract("probe")), encoding="utf-8")
        start = shlex.join([sys.executable, str(SCRIPT), "start"])
        check = (
            f"{start} < {shlex.quote(str(child_contract))} >/dev/null 2>&1; "
            'test "$?" -eq 2'
        )
        root = self.invoke(
            [sys.executable, SCRIPT, "start"],
            self.contract("root", check=check),
            environment=self.environment(session_id="verify-session"),
        )
        verified = subprocess.run(
            ["/bin/sh", "-c", root["verify_script"]],
            text=True,
            capture_output=True,
            cwd=self.root,
            env=self.environment(session_id="verify-session"),
            timeout=30,
        )
        self.assertIn("UNTIL-LOOP-RECORDED 1 checks for this action: all passed", verified.stdout)
        recovered = self.invoke(
            [sys.executable, SCRIPT, "next"],
            environment=self.environment(session_id="verify-session"),
        )
        self.assertEqual(recovered["stack"]["depth"], 1)
        self.assertEqual(recovered["work"], root["work"])

    def test_deleted_child_workspace_can_be_cancelled_and_return_to_parent(self):
        path, key, parent = self.direct_start("parent")
        child_workspace = self.root / "child-workspace"
        child_workspace.mkdir()
        _, _, child = self.direct_start("child", path=path, key=key, workspace=child_workspace)
        shutil.rmtree(child_workspace)

        resumed = sessions.done(
            runtime,
            path,
            self.token(child),
            self.report(assessment="unknown", continuation="cancelled", classification="unresolved"),
        )
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["work"], parent["work"])
        self.assertEqual(resumed["stack"]["depth"], 1)
        self.assertEqual(resumed["stack"]["child_results"][-1]["packet"]["status"], "stopped")

    def test_late_record_stream_rejects_after_child_push_and_pop(self):
        path, key, parent = self.direct_start("parent", check="true")
        parent_token = self.token(parent)

        class ChildDuringRead:
            triggered = False

            def read(inner, _size):
                if inner.triggered:
                    return b""
                inner.triggered = True
                child = sessions.start(runtime, path, key, self.contract("child"))
                sessions.done(runtime, path, self.token(child), self.report())
                return b"=== until-loop check artifact\nUNTIL-LOOP-CHECK artifact exit=0\n"

        with self.assertRaises(runtime.StateError) as error:
            sessions.record(
                runtime,
                path,
                parent_token,
                ChildDuringRead(),
                io.StringIO(),
            )
        self.assertIn("action token does not match", str(error.exception))
        recovered = sessions.next_packet(runtime, path)
        self.assertEqual(recovered["stack"]["depth"], 1)
        self.assertEqual(recovered["work"], parent["work"])
        self.assertEqual(len(recovered["stack"]["child_results"]), 1)

    def test_replace_fault_leaves_previous_stack_bytes_unchanged(self):
        path, _, packet = self.direct_start("root")
        before = path.read_bytes()

        with mock.patch.object(sessions.os, "replace", side_effect=OSError("injected replace fault")):
            with self.assertRaisesRegex(OSError, "injected replace fault"):
                sessions.checkpoint(runtime, path, self.token(packet), {"handoff": "partial progress"})

        self.assertEqual(path.read_bytes(), before)
        recovered = sessions.next_packet(runtime, path)
        self.assertIsNone(recovered["stack"]["checkpoint"])

    def test_directory_fsync_fault_keeps_committed_pop_recoverable(self):
        path, key, parent = self.direct_start("parent")
        _, _, child = self.direct_start("child", path=path, key=key)
        before = path.read_bytes()
        real_fsync = sessions.os.fsync
        calls = 0

        def fail_directory_fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync fault")
            return real_fsync(fd)

        with mock.patch.object(sessions.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(OSError, "injected directory fsync fault"):
                sessions.done(runtime, path, self.token(child), self.report())

        self.assertNotEqual(path.read_bytes(), before)
        recovered = sessions.next_packet(runtime, path)
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["work"], parent["work"])
        self.assertEqual(recovered["stack"]["depth"], 1)
        self.assertEqual(recovered["stack"]["child_results"][-1]["packet"]["status"], "complete")

    def test_malformed_terminal_stack_returns_structured_error(self):
        path, _, root = self.direct_start("root")
        sessions.done(runtime, path, self.token(root), self.report())
        document = json.loads(path.read_text())
        document["terminal"]["stack"] = []
        path.write_text(json.dumps(document))
        before = path.read_bytes()
        error = self.invoke([sys.executable, SCRIPT, "next", "--state", path], returncode=2)
        self.assertEqual(error["status"], "error")
        self.assertIn("terminal seal", error["error"])
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_session_envelope_is_refused_without_overwrite(self):
        path, key, _ = self.direct_start("root")
        corrupt = b"{not valid session JSON"
        path.write_bytes(corrupt)

        with self.assertRaises(runtime.StateError):
            sessions.start(runtime, path, key, self.contract("replacement"))

        self.assertEqual(path.read_bytes(), corrupt)


if __name__ == "__main__":
    unittest.main()
