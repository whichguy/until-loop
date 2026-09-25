"""Exercise the independently relocated marketplace package and generated parity."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plugin_views", ROOT / "scripts" / "sync_plugin_views.py"
)
PACKAGING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGING)


class PluginPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relocated-plugin-")
        self.root = Path(self.temporary.name).resolve()
        self.foreign = self.root / "unrelated cwd"
        self.foreign.mkdir()
        self.environment = {
            "PATH": os.defpath,
            "LANG": "C", "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, arguments, *, cwd=None, payload=None, expected_returncode=0):
        result = subprocess.run(
            [str(value) for value in arguments], cwd=cwd or self.foreign,
            env=self.environment, input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
        self.assertEqual(
            result.returncode,
            expected_returncode,
            result.stderr.decode(errors="replace"),
        )
        return result

    def relocated(self, name):
        destination = self.root / "independent cache" / name
        shutil.copytree(ROOT / "plugins" / name, destination)
        return destination

    @staticmethod
    def card_name(card):
        lines = card.read_text(encoding="utf-8").splitlines()
        try:
            start = lines.index("---")
            end = lines.index("---", start + 1)
        except ValueError as error:
            raise AssertionError("missing front matter: {}".format(card)) from error
        for line in lines[start + 1:end]:
            if line.startswith("name: "):
                return line.removeprefix("name: ").strip()
        raise AssertionError("missing skill name: {}".format(card))

    def recursively_discovered_card_names(self, skills_directory):
        """Model the recursive local-card scan below each installed skill entry."""
        return [
            self.card_name(card)
            for installed in sorted(skills_directory.iterdir())
            if installed.is_dir()
            for card in sorted(installed.rglob("SKILL.md"))
        ]

    def canonical_improve_card(self):
        card = self.root / "canonical Improve" / "SKILL.md"
        card.parent.mkdir()
        card.write_text(
            "---\nname: improve\ndescription: Canonical Improve fixture.\n---\n",
            encoding="utf-8",
        )
        return card

    @staticmethod
    def callback_report(
        classification,
        exit_assessment,
        continuation_assessment,
        evidence,
        handoff="Current candidate, checks and remaining gaps are recorded in this report.",
    ):
        return {
            "classification": classification,
            "exit_assessment": exit_assessment,
            "continuation_assessment": continuation_assessment,
            "evidence": evidence,
            "handoff": handoff,
        }

    def callback_contract(self, workspace, *, required_trivial_reviews=2):
        return {
            "workspace": str(workspace),
            "work": "Review the scoped candidate, make authorized improvements, and run checks.",
            "exit_condition": "Current evidence establishes the requested result.",
            "repeat_condition": "Useful authorized work remains while the result is unproven.",
            "required_trivial_reviews": required_trivial_reviews,
            "context": self.callback_context(workspace),
        }

    def callback_context(self, workspace):
        """Return frozen, concrete cold-context facts for a relocated package run."""
        baseline = workspace / "frozen baseline.txt"
        candidate = workspace / "candidate.md"
        excluded = workspace / "excluded generated output.md"
        policy = workspace / "review policy.md"
        record = workspace / "required evidence record.md"
        baseline.write_text("HEAD: seed-candidate\n")
        candidate.write_text("Candidate under review.\n")
        excluded.write_text("Do not edit this generated output.\n")
        policy.write_text("Review the frozen candidate before making an authorized change.\n")
        record.write_text("Record each completed iteration here.\n")
        return {
            "request": "Improve the frozen candidate and leave a recoverable review account.",
            "scope": (
                f"Frozen baseline: {baseline}. Include only {candidate}; exclude {excluded}. "
                "Preserve all other files."
            ),
            "authority": (
                "A material finding may be committed locally with its evidence; do not push, "
                "publish, change remotes, or alter the excluded output."
            ),
            "environment": (
                f"Work in {workspace}; the package is independently relocated and the state "
                "file is an ephemeral callback handle."
            ),
            "resources": [
                {"purpose": "frozen candidate baseline", "locator": str(baseline)},
                {"purpose": "review policy", "locator": str(policy)},
                {"purpose": "required evidence record", "locator": str(record)},
            ],
        }

    def callback_start(self, runtime, contract, state_directory, *, cwd=None):
        result = self.run_command(
            [sys.executable, runtime, "start", "--directory", state_directory],
            cwd=cwd,
            payload=json.dumps(contract).encode(),
        )
        packet = json.loads(result.stdout)
        self.assertEqual(packet["status"], "active")
        self.assertEqual(packet["workspace"], contract["workspace"])
        self.assertEqual(packet["work"], contract["work"])
        self.assertEqual(
            packet["conditions"],
            {"exit": contract["exit_condition"], "repeat": contract["repeat_condition"]},
        )
        return packet

    def callback_done(self, packet, report, *, expected_returncode=0, cwd=None):
        result = self.run_command(
            packet["done_argv"],
            cwd=cwd,
            payload=json.dumps(report).encode(),
            expected_returncode=expected_returncode,
        )
        return json.loads(result.stdout)

    def assert_relocated_context_continuation(self, runtime, package_name):
        """Prove a cold process can continue solely from a returned package packet."""
        runtime = Path(runtime).resolve()
        self.assertIn(" ", str(runtime))
        workspace = self.root / (package_name + " context workspace")
        workspace.mkdir()
        state_directory = self.root / (package_name + " context state")
        state_directory.mkdir()
        contract = self.callback_contract(workspace, required_trivial_reviews=0)
        context = contract["context"]

        first = self.callback_start(runtime, contract, state_directory)
        state_file = Path(first["state_file"])
        expected_next_argv = [
            str(Path(os.path.abspath(sys.executable))),
            str(runtime),
            "next",
            "--state",
            str(state_file),
        ]
        self.assertEqual(first["context"], context)
        self.assertIsNone(first["last_report"])
        self.assertEqual(first["next_argv"], expected_next_argv)
        self.assertIn("handoff", first["report_schema"]["required"])
        self.assertIn("context", first["instruction"])
        self.assertIn("next_argv", first["instruction"])

        before_missing_handoff = state_file.read_bytes()
        handoffless_report = self.callback_report(
            "non-trivial", "unsatisfied", "allowed", "A real finding lacks a handoff."
        )
        del handoffless_report["handoff"]
        missing_handoff = self.callback_done(
            first, handoffless_report, expected_returncode=2
        )
        self.assertEqual(missing_handoff["state_change"], "unchanged")
        self.assertIn("report.handoff is required", missing_handoff["error"])
        self.assertEqual(missing_handoff["state_file"], str(state_file))
        self.assertEqual(missing_handoff["next_argv"], expected_next_argv)
        self.assertEqual(state_file.read_bytes(), before_missing_handoff)

        first_handoff = "First-only fact: the candidate needed one material correction."
        successor = self.callback_done(
            first,
            self.callback_report(
                "non-trivial",
                "unsatisfied",
                "allowed",
                "Corrected the material finding and completed its requested check.",
                first_handoff,
            ),
        )
        self.assertEqual(successor["status"], "active")
        self.assertEqual(successor["context"], context)
        self.assertEqual(successor["last_report"]["handoff"], first_handoff)
        self.assertEqual(successor["next_argv"], expected_next_argv)

        stale_done_argv = list(first["done_argv"])
        saved_packet_path = self.root / (package_name + " returned packet.json")
        saved_packet_path.write_text(json.dumps(successor, sort_keys=True))
        saved_successor = json.loads(saved_packet_path.read_text())
        self.assertNotEqual(saved_successor["done_argv"], stale_done_argv)
        del first
        del successor
        before_rehydrate = state_file.read_bytes()
        rehydrated_result = self.run_command(saved_successor["next_argv"], cwd=self.foreign)
        rehydrated = json.loads(rehydrated_result.stdout)
        self.assertEqual(rehydrated, saved_successor)
        self.assertEqual(state_file.read_bytes(), before_rehydrate)

        stale = self.run_command(
            stale_done_argv,
            payload=json.dumps(
                self.callback_report(
                    "non-trivial",
                    "unsatisfied",
                    "allowed",
                    "A stale callback must not advance the current action.",
                    "Stale callback handoff.",
                )
            ).encode(),
            expected_returncode=2,
        )
        stale_packet = json.loads(stale.stdout)
        self.assertEqual(stale_packet["state_change"], "unchanged")
        self.assertEqual(stale_packet["state_file"], str(state_file))
        self.assertEqual(stale_packet["next_argv"], expected_next_argv)
        self.assertEqual(state_file.read_bytes(), before_rehydrate)

        second_handoff = (
            "Second-only fact: the terminal review explicitly carries the current check result."
        )
        complete = self.callback_done(
            rehydrated,
            self.callback_report(
                "trivial",
                "satisfied",
                "allowed",
                "A distinct clean review established the exit condition.",
                second_handoff,
            ),
        )
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["context"], context)
        self.assertEqual(complete["last_report"]["handoff"], second_handoff)
        self.assertNotIn(first_handoff, json.dumps(complete, sort_keys=True))
        self.assertIsNone(complete["next_argv"])
        self.assertIsNone(complete["done_argv"])
        self.assertFalse(state_file.exists())

        stopped = self.callback_start(runtime, contract, state_directory)
        stopped_state = Path(stopped["state_file"])
        stopped_packet = self.callback_done(
            stopped,
            self.callback_report(
                "unresolved",
                "unknown",
                "cancelled",
                "A later explicit stop ended this iteration before completion.",
                "Stop handoff: no further candidate work is authorized.",
            ),
        )
        self.assertEqual(stopped_packet["status"], "stopped")
        self.assertEqual(stopped_packet["context"], context)
        self.assertEqual(
            stopped_packet["last_report"]["handoff"],
            "Stop handoff: no further candidate work is authorized.",
        )
        self.assertIsNone(stopped_packet["next_argv"])
        self.assertIsNone(stopped_packet["done_argv"])
        self.assertFalse(stopped_state.exists())
        self.assertEqual(list(state_directory.iterdir()), [])

    def assert_relocated_callback_protocol(self, runtime, package_name):
        """Exercise a package-local callback chain without shared workspace state."""
        workspace = self.root / (package_name + " callback workspace")
        workspace.mkdir()
        state_directory = self.root / (package_name + " callback state")
        state_directory.mkdir()
        self.assertFalse((workspace / ".until-loop").exists())
        contract = self.callback_contract(workspace)

        first = self.callback_start(runtime, contract, state_directory)
        second = self.callback_start(runtime, contract, state_directory)
        first_state = Path(first["state_file"])
        second_state = Path(second["state_file"])
        self.assertNotEqual(first_state, second_state)
        self.assertEqual(first_state.parent, state_directory)
        self.assertEqual(second_state.parent, state_directory)

        first_argv = first["done_argv"]
        self.assertTrue(Path(first_argv[0]).is_absolute())
        self.assertTrue(os.path.samefile(first_argv[0], sys.executable))
        self.assertEqual(first_argv[1], str(Path(runtime).resolve()))
        self.assertEqual(first_argv[2:4], ["done", "--state"])
        self.assertEqual(first_argv[4], str(first_state))
        self.assertEqual(len(first_argv), 6)
        self.assertTrue(first_argv[5].startswith("--action="))
        self.assertGreater(len(first_argv[5]), len("--action="))

        next_packet = self.run_command(
            [sys.executable, runtime, "next", "--state", first_state]
        )
        self.assertEqual(json.loads(next_packet.stdout), first)
        first_before = first_state.read_bytes()
        second_before = second_state.read_bytes()
        wrong_run_argv = list(first_argv)
        wrong_run_argv[wrong_run_argv.index("--state") + 1] = str(second_state)
        cross_run = self.run_command(
            wrong_run_argv,
            payload=json.dumps(
                self.callback_report(
                    "non-trivial", "unsatisfied", "allowed", "Wrong run must be rejected."
                )
            ).encode(),
            expected_returncode=2,
        )
        self.assertEqual(json.loads(cross_run.stdout)["state_change"], "unchanged")
        self.assertEqual(first_state.read_bytes(), first_before)
        self.assertEqual(second_state.read_bytes(), second_before)

        after_material = self.callback_done(
            first,
            self.callback_report(
                "non-trivial", "unsatisfied", "allowed", "A material finding was corrected and rechecked."
            ),
        )
        self.assertEqual(after_material["status"], "active")
        self.assertEqual(after_material["progress"]["action_number"], 2)
        self.assertEqual(after_material["progress"]["trivial_streak"], 0)
        after_first_clean = self.callback_done(
            after_material,
            self.callback_report(
                "trivial", "unsatisfied", "allowed", "The first distinct clean review found no material change."
            ),
        )
        self.assertEqual(after_first_clean["status"], "active")
        self.assertEqual(after_first_clean["progress"]["action_number"], 3)
        self.assertEqual(after_first_clean["progress"]["trivial_streak"], 1)
        complete = self.callback_done(
            after_first_clean,
            self.callback_report(
                "trivial", "satisfied", "allowed", "The second distinct clean review established the exit."
            ),
        )
        self.assertEqual(complete["status"], "complete")
        self.assertFalse(first_state.exists())

        second_after_first_clean = self.callback_done(
            second,
            self.callback_report(
                "trivial", "unsatisfied", "allowed", "Independent first clean review."
            ),
        )
        second_complete = self.callback_done(
            second_after_first_clean,
            self.callback_report(
                "trivial", "satisfied", "allowed", "Independent second clean review."
            ),
        )
        self.assertEqual(second_complete["status"], "complete")
        self.assertFalse(second_state.exists())
        self.assertEqual(list(state_directory.iterdir()), [])
        self.assertFalse((workspace / ".until-loop").exists())

    def test_generated_views_match_sources_and_host_versions(self):
        self.run_command([sys.executable, ROOT / "scripts/sync_plugin_views.py", "--check"])
        version_line = "version: " + PACKAGING.VERSION
        self.assertIn(version_line, (ROOT / "SKILL.md").read_text().splitlines())
        for name, mapping in PACKAGING.source_mappings().items():
            with self.subTest(plugin=name):
                plugin = ROOT / "plugins" / name
                self.assertFalse(any(path.is_symlink() for path in plugin.rglob("*")))
                for destination, source in mapping.items():
                    self.assertEqual((plugin / destination).read_bytes(), (ROOT / source).read_bytes())
                for host in (".claude-plugin", ".codex-plugin"):
                    manifest = json.loads((plugin / host / "plugin.json").read_text())
                    self.assertEqual(manifest["name"], name)
                    self.assertEqual(manifest["version"], PACKAGING.VERSION)

    def test_local_skill_link_discovers_one_until_loop_beside_canonical_improve(self):
        canonical_improve = self.canonical_improve_card().parent
        bad_skills = self.root / "whole checkout home/.codex/skills"
        bad_skills.mkdir(parents=True)
        (bad_skills / "improve").symlink_to(canonical_improve, target_is_directory=True)
        (bad_skills / "until-loop").symlink_to(ROOT, target_is_directory=True)
        duplicate_names = self.recursively_discovered_card_names(bad_skills)
        self.assertGreater(duplicate_names.count("until-loop"), 1)

        good_skills = self.root / "generated card home/.codex/skills"
        good_skills.mkdir(parents=True)
        installed_improve = good_skills / "improve"
        installed_improve.symlink_to(canonical_improve, target_is_directory=True)
        original_improve_target = os.readlink(installed_improve)
        package = self.relocated("until-loop")
        installed = good_skills / "until-loop"
        installed.symlink_to(package / "skills/until-loop", target_is_directory=True)
        self.assertEqual(
            self.recursively_discovered_card_names(good_skills),
            ["improve", "until-loop"],
        )

        selected_card = installed / "SKILL.md"
        runtime = installed / "scripts/until_loop_ephemeral.py"
        self.assertEqual(selected_card.resolve(), package / "skills/until-loop/SKILL.md")
        self.assertEqual(
            runtime.resolve().relative_to(package),
            Path("skills/until-loop/scripts/until_loop_ephemeral.py"),
        )
        self.assertEqual(os.readlink(installed_improve), original_improve_target)
        self.assertEqual(installed_improve.resolve(), canonical_improve)
        workspace = self.root / "installed symlink workspace"
        workspace.mkdir()
        state_directory = self.root / "installed symlink state"
        state_directory.mkdir()
        packet = self.callback_start(
            runtime,
            self.callback_contract(workspace, required_trivial_reviews=0),
            state_directory,
            cwd=self.foreign,
        )
        state_file = Path(packet["state_file"])
        terminal = self.callback_done(
            packet,
            self.callback_report(
                "non-trivial", "satisfied", "allowed",
                "The installed symlink runtime completed from an unrelated cwd.",
            ),
            cwd=self.foreign,
        )
        self.assertEqual(terminal["status"], "complete")
        self.assertFalse(state_file.exists())
        self.assertEqual(list(self.foreign.iterdir()), [])

    def test_until_loop_ephemeral_callbacks_after_isolated_relocation(self):
        package = self.relocated("until-loop")
        runtime = package / "skills/until-loop/scripts/until_loop_ephemeral.py"
        self.assert_relocated_callback_protocol(runtime, "until-loop")
        self.assert_relocated_context_continuation(runtime, "until-loop")

    def test_check_rejects_missing_extra_modified_and_linked_package_files(self):
        package = self.relocated("until-loop")
        files = PACKAGING.expected_views(ROOT)["until-loop"]
        self.assertEqual(PACKAGING.check_view(package, files), [])
        target = package / "skills/until-loop/SKILL.md"
        original = target.read_bytes()
        target.write_bytes(original + b"\nUnexpected edit\n")
        self.assertTrue(PACKAGING.check_view(package, files))
        target.unlink()
        self.assertTrue(PACKAGING.check_view(package, files))
        outside = self.root / "outside.md"
        outside.write_bytes(original)
        target.symlink_to(outside)
        self.assertTrue(PACKAGING.check_view(package, files))
        self.assertEqual(outside.read_bytes(), original)
        target.unlink()
        target.write_bytes(original)
        (package / "unexpected.txt").write_text("Untracked generated content")
        self.assertTrue(PACKAGING.check_view(package, files))

    def test_sync_rejects_linked_plugin_parent_without_touching_target(self):
        source = self.root / "source"
        source.mkdir()
        for mapping in PACKAGING.source_mappings().values():
            for relative in mapping.values():
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
        outside = self.root / "outside plugins"
        outside.mkdir()
        sentinel = outside / "preserve.txt"
        sentinel.write_text("User-owned file")
        (source / "plugins").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PACKAGING.PackagingError):
            PACKAGING.sync(source)
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "User-owned file")


if __name__ == "__main__":
    unittest.main()
