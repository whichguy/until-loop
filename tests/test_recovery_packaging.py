"""Exercise generated recovery hooks from an independently relocated package."""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts" / "until_loop_ephemeral.py"
PACKAGING_SPEC = importlib.util.spec_from_file_location(
    "plugin_views", ROOT / "scripts" / "sync_plugin_views.py"
)
PACKAGING = importlib.util.module_from_spec(PACKAGING_SPEC)
PACKAGING_SPEC.loader.exec_module(PACKAGING)
IDENTITY_ENV = (
    "UNTIL_LOOP_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GROK_SESSION_ID",
)
PACKET_MARKER = "Until Loop recovery packet:\n"


class RecoveryPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relocated-recovery-plugin-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.package = self.root / "relocated plugin with spaces"
        self.foreign_cwd = self.root / "unrelated cwd"
        self.state_dir = self.root / "session state"
        self.foreign_cwd.mkdir()
        self.state_dir.mkdir()
        for relative, planned in PACKAGING.expected_views(ROOT)["until-loop"].items():
            destination = self.package / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(planned.contents)
            os.chmod(destination, planned.mode)

        self.environment = dict(os.environ)
        for name in IDENTITY_ENV:
            self.environment.pop(name, None)
        self.environment.pop("UNTIL_LOOP_STATE_DIR", None)
        self.environment.update(
            {
                "PATH": str(Path(sys.executable).resolve().parent) + os.pathsep + os.defpath,
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "UNTIL_LOOP_STATE_DIR": str(self.state_dir),
            }
        )

    def run_command(self, argv, *, payload=None, environment=None, cwd=None):
        result = subprocess.run(
            [str(value) for value in argv],
            input=None if payload is None else json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment if environment is None else environment,
            cwd=self.foreign_cwd if cwd is None else cwd,
            timeout=30,
            check=False,
        )
        return result

    def contract(self, name, *, request_padding=0):
        return {
            "workspace": str(self.foreign_cwd),
            "work": f"Complete the relocated package {name} work and check its artifact.",
            "exit_condition": f"The current evidence for {name} is sufficient.",
            "repeat_condition": "Continue while authorized work remains and stop on an unmet dependency.",
            "required_trivial_reviews": 0,
            "context": {
                "request": f"Complete {name} and retain a recovery packet." + ("x" * request_padding),
                "scope": f"Only the temporary workspace and {name} artifact.",
                "authority": "Local fixture writes and checks only.",
                "environment": "Recover through the generated relocated package hook.",
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
        return {"GROK_SESSION_ID": session_id}

    def start_source_session(self, host, session_id, *, request_padding=0):
        result = self.run_command(
            [sys.executable, RUNTIME, "start"],
            payload=self.contract(host, request_padding=request_padding),
            environment={**self.environment, **self.native_identity(host, session_id)},
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
    def commands_in(value):
        if isinstance(value, dict):
            found = []
            for key, item in value.items():
                if key == "command" and isinstance(item, str):
                    found.append(item)
                else:
                    found.extend(RecoveryPackagingTests.commands_in(item))
            return found
        if isinstance(value, list):
            return [command for item in value for command in RecoveryPackagingTests.commands_in(item)]
        return []

    @staticmethod
    def values_for_key(value, name):
        if isinstance(value, dict):
            found = [value[name]] if name in value else []
            return found + [
                item_value
                for item in value.values()
                for item_value in RecoveryPackagingTests.values_for_key(item, name)
            ]
        if isinstance(value, list):
            return [
                item_value
                for item in value
                for item_value in RecoveryPackagingTests.values_for_key(item, name)
            ]
        return []

    def hook_command(self, host, event_name=None):
        if host == "codex":
            manifest = json.loads((self.package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["hooks"], "./hooks/codex.json")
            hook_file = self.package / "hooks" / "codex.json"
            root_variable = "PLUGIN_ROOT"
            quoted_root = '"$PLUGIN_ROOT/skills/until-loop/scripts/until_loop_recover.py"'
        else:
            hook_file = self.package / "hooks" / "hooks.json"
            root_variable = "CLAUDE_PLUGIN_ROOT"
            quoted_root = '"${CLAUDE_PLUGIN_ROOT}/skills/until-loop/scripts/until_loop_recover.py"'

        self.assertTrue(hook_file.is_file(), f"generated hook file is missing: {hook_file}")
        hook_document = json.loads(hook_file.read_text(encoding="utf-8"))
        if host == "codex":
            self.assertEqual(self.values_for_key(hook_document, "additionalContextLimit"), [0])
        host_commands = [command for command in self.commands_in(hook_document) if f"--host {host}" in command]
        if host == "grok":
            self.assertEqual(
                {"SessionStart", "PreCompact", "PostCompact", "PreToolUse", "Stop"},
                {name for name, entries in hook_document["hooks"].items()
                 if any("--host grok" in command for command in self.commands_in(entries))},
            )
            self.assertEqual(len(host_commands), 5, "expected one Grok recovery command per native event")
            for grok_command in host_commands:
                self.assertIn('[ -n "$GROK_HOOK_EVENT" ] || exit 0;', grok_command)
                self.assertIn(quoted_root, grok_command)
            self.assertTrue(event_name, "select a Grok event to execute")
            host_commands = [command for command in self.commands_in(hook_document["hooks"][event_name])
                             if "--host grok" in command]
        self.assertEqual(len(host_commands), 1, f"expected one {host} recovery command")
        command = host_commands[0]
        self.assertIn(quoted_root, command)
        return command, root_variable

    @staticmethod
    def hook_context(result):
        response = json.loads(result.stdout)
        return response["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def context_packet(context):
        position = context.find(PACKET_MARKER)
        if position < 0:
            raise AssertionError("recovery context is missing its packet marker")
        return json.loads(context[position + len(PACKET_MARKER):].strip())

    @staticmethod
    def fallback_argv(context):
        prompt = (
            "execute this exact read-only argv to retrieve the COMPLETE current packet; "
            "do not start a new loop or use callbacks from the summary:\n"
        )
        if prompt not in context:
            raise AssertionError("bounded recovery context is missing the exact next argv instruction")
        encoded = context.split(prompt, 1)[1].split("\nRead the returned", 1)[0]
        argv = json.loads(encoded)
        if not isinstance(argv, list) or not argv:
            raise AssertionError("recovery next argv must be a nonempty JSON array")
        return argv

    def test_generated_codex_and_claude_commands_work_after_relocation(self):
        self.assertTrue((self.package / "hooks" / "codex.json").is_file())
        self.assertTrue((self.package / "hooks" / "hooks.json").is_file())
        self.assertTrue(" " in str(self.package))
        relocated_runtime = self.package / "skills" / "until-loop" / "scripts" / "until_loop_ephemeral.py"
        relocated_recovery = self.package / "skills" / "until-loop" / "scripts" / "until_loop_recover.py"
        self.assertTrue(relocated_runtime.is_file())
        self.assertTrue(relocated_recovery.is_file())

        for host in ("codex", "claude"):
            with self.subTest(host=host):
                session_id = f"relocated-{host}-native-session"
                request_padding = 12_000 if host == "claude" else 0
                source_packet = self.start_source_session(
                    host,
                    session_id,
                    request_padding=request_padding,
                )
                command, root_variable = self.hook_command(host)
                environment = {
                    **self.environment,
                    **self.native_identity(host, session_id),
                    root_variable: str(self.package),
                }
                event = {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": session_id,
                }
                hook_environment = dict(environment)
                hook_environment.pop("PYTHONDONTWRITEBYTECODE", None)
                result = self.run_command(
                    ["/bin/sh", "-c", command],
                    payload=event,
                    environment=hook_environment,
                    cwd=self.foreign_cwd,
                )
                self.assertEqual(list(self.package.rglob("__pycache__")), [])

                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"),
                )
                context = self.hook_context(result)
                if host == "claude":
                    self.assertNotIn(PACKET_MARKER, context)
                    self.assertLessEqual(len(context.encode("utf-8")), 9_500)
                    next_argv = self.fallback_argv(context)
                    self.assertIn(str(relocated_runtime), next_argv[1])
                    next_result = self.run_command(
                        next_argv,
                        environment=environment,
                        cwd=self.foreign_cwd,
                    )
                    self.assertEqual(
                        next_result.returncode,
                        0,
                        next_result.stdout.decode(errors="replace")
                        + next_result.stderr.decode(errors="replace"),
                    )
                    packet = json.loads(next_result.stdout)
                else:
                    packet = self.context_packet(context)
                self.assertEqual(packet["work"], source_packet["work"])
                self.assertIn(str(relocated_runtime), " ".join(packet["done_argv"]))
                callback = " ".join(packet["done_argv"])
                self.assertNotIn(str(RUNTIME), callback)

    def test_generated_grok_hooks_arm_and_deliver_after_relocation(self):
        self.assertTrue(" " in str(self.package))
        relocated_runtime = self.package / "skills" / "until-loop" / "scripts" / "until_loop_ephemeral.py"
        relocated_recovery = self.package / "skills" / "until-loop" / "scripts" / "until_loop_recover.py"
        self.assertTrue(relocated_runtime.is_file())
        self.assertTrue(relocated_recovery.is_file())

        session_id = "relocated-grok-native-session"
        source_packet = self.start_source_session("grok", session_id)
        _, claude_root_variable = self.hook_command("claude")
        self.assertEqual(claude_root_variable, "CLAUDE_PLUGIN_ROOT")

        arm_command, root_variable = self.hook_command("grok", "PreCompact")
        self.assertEqual(root_variable, "CLAUDE_PLUGIN_ROOT")
        environment = {
            **self.environment,
            **self.native_identity("grok", session_id),
            root_variable: str(self.package),
            "GROK_HOOK_EVENT": "pre_compact",
        }
        arm_event = {
            "hookEventName": "pre_compact",
            "hook_event_name": "PreCompact",
            "sessionId": session_id,
            "session_id": session_id,
            "timestamp": "2026-10-17T19:00:00+00:00",
            "source": "manual",
        }
        armed = self.run_command(
            ["/bin/sh", "-c", arm_command],
            payload=arm_event,
            environment=environment,
            cwd=self.foreign_cwd,
        )
        self.assertEqual(
            armed.returncode,
            0,
            armed.stdout.decode(errors="replace") + armed.stderr.decode(errors="replace"),
        )
        self.assertEqual(armed.stdout, b"")

        deliver_command, _ = self.hook_command("grok", "PreToolUse")
        delivery_environment = {**environment, "GROK_HOOK_EVENT": "pre_tool_use"}
        delivery_event = {
            "hookEventName": "pre_tool_use",
            "hook_event_name": "PreToolUse",
            "sessionId": session_id,
            "session_id": session_id,
            "timestamp": "2026-10-17T19:00:01+00:00",
            "toolName": "run_terminal_command",
            "tool_name": "run_terminal_command",
        }
        delivered = self.run_command(
            ["/bin/sh", "-c", deliver_command],
            payload=delivery_event,
            environment=delivery_environment,
            cwd=self.foreign_cwd,
        )
        self.assertEqual(
            delivered.returncode,
            0,
            delivered.stdout.decode(errors="replace") + delivered.stderr.decode(errors="replace"),
        )
        response = json.loads(delivered.stdout)
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["permissionDecision"], "deny")
        context = hook_output["permissionDecisionReason"]
        marker = context.find(PACKET_MARKER)
        if marker >= 0:
            packet = json.loads(context[marker + len(PACKET_MARKER):].strip())
        else:
            self.assertLessEqual(len(context.encode("utf-8")), 9_500)
            next_argv = self.fallback_argv(context)
            self.assertIn(str(relocated_runtime), next_argv[1])
            fetched = self.run_command(
                next_argv,
                environment=delivery_environment,
                cwd=self.foreign_cwd,
            )
            self.assertEqual(
                fetched.returncode,
                0,
                fetched.stdout.decode(errors="replace") + fetched.stderr.decode(errors="replace"),
            )
            packet = json.loads(fetched.stdout)
        self.assertEqual(packet["work"], source_packet["work"])
        self.assertIn(str(relocated_runtime), " ".join(packet["done_argv"]))
        self.assertNotIn(str(RUNTIME), " ".join(packet["done_argv"]))
        self.assertEqual(list(self.package.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main()
