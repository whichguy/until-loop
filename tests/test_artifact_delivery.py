"""Exercise the real CLI against a versioned release fixture and an opt-in live race.

The fixture owns immutable versions and the final handoff identity check. The CLI
supplies the frozen commands to the verifier shell and records their results.

Run this file with --probe to see the live workspace observation as JSON. Add
--strict to fail if the CLI completes while the clauses never hold together.
"""

import argparse
import hashlib
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "scripts" / "until_loop_ephemeral.py"
TIMEOUT = 15
REQUIREMENTS = {
    "format": 2,
    "license": "MIT",
    "revision": "approved-revision",
    "tests": "passed",
    "target": "linux-x64",
    "debug": False,
}
CHECK_IDS = [f"C{i}" for i in range(1, len(REQUIREMENTS) + 1)]
HANDOFF_CHECK = "HANDOFF"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def python_command(source, *args):
    return shlex.join([sys.executable, "-c", source, *map(str, args)])


class Fixture:
    def __init__(self, root):
        self.root = root
        self.workspace = root / "work"
        self.workspace.mkdir()
        self.env = {
            "HOME": str(root),
            "TMPDIR": str(root),
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        }

    @contextmanager
    def process(self, argv, stdin=None):
        with subprocess.Popen(
            argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            cwd=self.workspace, env=self.env,
        ) as proc:
            try:
                yield proc
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def run(self, argv, payload=None):
        with self.process(argv, stdin=subprocess.PIPE if payload is not None else None) as proc:
            stdout, stderr = proc.communicate(payload, timeout=TIMEOUT)
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    def start(self, checks):
        contract = {
            "workspace": str(self.workspace),
            "work": "Qualify the release fixture.",
            "exit_condition": "Every release clause holds for the artifact being handed off.",
            "repeat_condition": "Continue while qualification is incomplete and progress is possible.",
            "required_trivial_reviews": 0,
            "context": {"request": "Qualify release", "scope": "Fixture only", "authority": "Local checks",
                        "environment": "Temporary workspace", "resources": []},
            "exit_checks": checks,
        }
        result = self.run([sys.executable, str(RUNTIME), "start", "--isolated"], json.dumps(contract))
        result.check_returncode()
        packet = json.loads(result.stdout)
        if packet["status"] != "active":
            raise AssertionError(packet)
        return packet

    def done(self, packet):
        report = {"classification": "non-trivial", "exit_assessment": "satisfied",
                  "continuation_assessment": "allowed", "evidence": "Ran the release checks.",
                  "handoff": "Release qualification fixture."}
        result = self.run([sys.executable, *packet["done_argv"][1:]], json.dumps(report))
        return json.loads(result.stdout)


class VersionedRelease:
    def __init__(self, root):
        self.store = root / "versions"
        self.store.mkdir()
        self.selection = root / "selected-version"

    def add_version(self, **fields):
        data = json.dumps({**REQUIREMENTS, **fields}, sort_keys=True).encode("ascii")
        version = digest(data)
        path = self.store / version
        with path.open("xb") as output:
            output.write(data)
        path.chmod(0o444)
        return version

    def select(self, version):
        self.selection.write_text(version, encoding="ascii")

    def checks(self, version):
        read_candidate = (
            "from pathlib import Path; from hashlib import sha256; import json, sys; "
            "data = Path(sys.argv[1]).read_bytes(); "
        )
        check_field = read_candidate + (
            "raise SystemExit(not (sha256(data).hexdigest() == sys.argv[2] "
            "and json.loads(data)[sys.argv[3]] == json.loads(sys.argv[4])))"
        )
        check_selection = read_candidate + (
            "raise SystemExit(not (sha256(data).hexdigest() == sys.argv[2] "
            "and Path(sys.argv[3]).read_text(encoding='ascii') == sys.argv[2]))"
        )
        path = self.store / version
        checks = [
            {"id": check_id, "clause": f"Release field {name} is {value!r}.",
             "command": python_command(check_field, path, version, name, json.dumps(value))}
            for check_id, (name, value) in zip(CHECK_IDS, REQUIREMENTS.items())
        ]
        return checks + [
            {"id": HANDOFF_CHECK, "clause": "The selected release matches the checked version and digest.",
             "command": python_command(check_selection, path, version, self.selection)},
        ]

    def handoff(self, qualified_version):
        """Return the verified bytes directly to the fixture's recipient."""
        if self.selection.read_text(encoding="ascii") != qualified_version:
            raise ValueError("delivery selection changed")
        data = (self.store / qualified_version).read_bytes()
        if digest(data) != qualified_version:
            raise ValueError("delivery digest changed")
        return data


class ArtifactDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="until-loop-release-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.fixture = Fixture(root)
        self.release = VersionedRelease(root)

    def qualify(self, packet, *, failing=()):
        verify = self.fixture.run(["/bin/sh", "-c", packet["verify_script"]])
        self.assertEqual(verify.returncode, 0, (verify.stdout, verify.stderr))
        outcome = self.fixture.done(packet)
        self.assertEqual(outcome["status"], "active" if failing else "complete", outcome)
        self.assertEqual(
            [(row["id"], row["passed"]) for row in outcome["last_check_results"]],
            [(check_id, check_id not in failing) for check_id in CHECK_IDS + [HANDOFF_CHECK]],
        )

    def test_checks_and_handoff_use_the_same_valid_release_version(self):
        valid = self.release.add_version()
        self.release.select(valid)
        packet = self.fixture.start(self.release.checks(valid))
        self.qualify(packet)
        delivered = self.release.handoff(valid)
        self.assertEqual(digest(delivered), valid)
        self.assertEqual(json.loads(delivered), REQUIREMENTS)

    def test_invalid_release_cannot_complete(self):
        invalid = self.release.add_version(tests="failed")
        self.release.select(invalid)
        packet = self.fixture.start(self.release.checks(invalid))
        self.qualify(packet, failing=("C4",))

    def test_switching_to_another_valid_version_fails_the_handoff_check(self):
        qualified = self.release.add_version(build="first")
        other = self.release.add_version(build="second")
        self.release.select(qualified)
        packet = self.fixture.start(self.release.checks(qualified))
        self.release.select(other)
        self.qualify(packet, failing=(HANDOFF_CHECK,))

    def test_fixture_handoff_rechecks_identity_after_cli_completion(self):
        qualified = self.release.add_version(build="first")
        other = self.release.add_version(build="second")
        self.release.select(qualified)
        packet = self.fixture.start(self.release.checks(qualified))
        self.qualify(packet)
        self.release.select(other)
        with self.assertRaisesRegex(ValueError, "delivery selection changed"):
            self.release.handoff(qualified)

    def test_fixture_handoff_rejects_different_bytes_under_the_same_version(self):
        qualified = self.release.add_version()
        other = self.release.add_version(build="replacement")
        self.release.select(qualified)
        packet = self.fixture.start(self.release.checks(qualified))
        self.qualify(packet)
        # Simulate a broken version store by replacing its read-only file.
        (self.release.store / other).replace(self.release.store / qualified)
        with self.assertRaisesRegex(ValueError, "delivery digest changed"):
            self.release.handoff(qualified)

    def test_live_workspace_probe_refuses_completion_when_workspace_mutates_mid_verify(self):
        result = live_workspace_probe()
        self.assertTrue(result["complete_implies_all_clauses_held_together"], result)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["verify_exit"], 2, result)
        self.assertEqual(result["invalid_by_stage"], [["C6"], ["C1", "C6"], ["C1"]], result)
        refusal = (result["refusal"] or "").lower()
        self.assertIn("changed", refusal)
        self.assertIn("workspace", refusal)


def live_workspace_probe():
    with tempfile.TemporaryDirectory(prefix="until-loop-live-") as directory:
        root = Path(directory).resolve()
        fixture = Fixture(root)
        files = [fixture.workspace / f"{check_id}.txt" for check_id in CHECK_IDS]
        for file in files[:-1]:
            file.write_text("ready", encoding="ascii")
        files[-1].write_text("pending", encoding="ascii")
        for args in (["init", "-q"], ["add", "."]):
            fixture.run(["git", *args]).check_returncode()

        def invalid():
            return [file.stem for file in files if file.read_text(encoding="ascii") != "ready"]

        waiting, proceed = root / "waiting", root / "proceed"
        os.mkfifo(waiting)
        os.mkfifo(proceed)
        checks = [{"id": file.stem, "clause": f"{file.name} is ready.",
                   "command": f'test "$(cat {shlex.quote(file.name)})" = ready'} for file in files]
        checks[-1]["command"] = (
            f'printf "waiting\\n" > {shlex.quote(str(waiting))} && '
            f'IFS= read -r event < {shlex.quote(str(proceed))} && ' + checks[-1]["command"]
        )
        packet = fixture.start(checks)
        states = [invalid()]
        # Open both ends in the parent so communication cannot block on FIFO open.
        with os.fdopen(os.open(waiting, os.O_RDWR | os.O_NONBLOCK), "rb", buffering=0) as ready, \
                os.fdopen(os.open(proceed, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as release, \
                fixture.process(["/bin/sh", "-c", packet["verify_script"]]) as proc:
            if not select.select([ready], [], [], TIMEOUT)[0] or ready.read(8) != b"waiting\n":
                raise AssertionError("verification never reached the sixth check")
            # The first five commands have read their files. Preserve at least one false clause at every write.
            files[0].write_text("pending", encoding="ascii")
            states.append(invalid())
            files[-1].write_text("ready", encoding="ascii")
            states.append(invalid())
            release.write(b"updated\n")
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        if states != [["C6"], ["C1", "C6"], ["C1"]]:
            raise AssertionError(states)
        outcome = fixture.done(packet)
        status = outcome["status"]
        recorded = outcome.get("last_check_results") or []
        recorded_passes = [row["id"] for row in recorded if row["passed"]]
        diagnostic = " ".join([str(outcome.get("error", "")), stdout, stderr]).lower()
        refused_change = ("changed" in diagnostic and "workspace" in diagnostic)
        failed_clause = any(row["id"] in ("C1", "C6") and not row["passed"] for row in recorded)
        if status == "complete":
            if proc.returncode != 0 or recorded_passes != CHECK_IDS:
                raise AssertionError((outcome, stdout, stderr))
        elif not (status in {"active", "error", "stopped"} and (refused_change or failed_clause)):
            raise AssertionError((outcome, stdout, stderr))
        return {"status": status, "invalid_by_stage": states,
                "recorded_passes": recorded_passes,
                "complete_implies_all_clauses_held_together": status != "complete",
                "verify_exit": proc.returncode, "verify_stdout": stdout, "verify_stderr": stderr,
                "refusal": (outcome.get("error") or outcome.get("reason")) if status != "complete" else None}


if __name__ == "__main__":
    if "--probe" in sys.argv:
        parser = argparse.ArgumentParser(description="Probe completion against a changing tracked workspace.")
        parser.add_argument("--probe", action="store_true", help="Run the live workspace probe.")
        parser.add_argument("--strict", action="store_true", help="Exit 1 for a false completion.")
        args = parser.parse_args()
        try:
            result = live_workspace_probe()
        except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as error:
            print(f"INCOMPLETE: {error}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(result, indent=2))
        sys.exit(int(args.strict and not result["complete_implies_all_clauses_held_together"]))
    unittest.main()
