"""Exercise independently relocated marketplace packages and generated parity."""
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
            "PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, arguments, *, cwd=None, payload=None):
        result = subprocess.run(
            [str(value) for value in arguments], cwd=cwd or self.foreign,
            env=self.environment, input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def relocated(self, name):
        destination = self.root / "independent cache" / name
        shutil.copytree(ROOT / "plugins" / name, destination)
        return destination

    def contract(self):
        return {
            "version": 1, "policy": "decision-rubric/2",
            "original_request": "Inspect notes.txt and preserve the user's work.",
            "interpretation": "Inspect the scoped candidate and assess its evidence.",
            "criteria": [{
                "id": "C1", "text": "The scoped candidate is inspected.",
                "basis": {"kind": "request", "reference": "Inspect notes.txt"},
            }],
        }

    def fixture(self):
        repo = self.root / "task repository"
        repo.mkdir()
        self.run_command(["git", "init", "-q", repo])
        self.run_command(["git", "config", "user.name", "Package Fixture"], cwd=repo)
        self.run_command(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo)
        (repo / "notes.txt").write_text("Preserve this content.\n")
        self.run_command(["git", "add", "notes.txt"], cwd=repo)
        self.run_command([
            "git", "commit", "--quiet", "--no-gpg-sign", "--no-verify",
            "-m", "Seed task", "-m", "Keep the full historical message.",
        ], cwd=repo)
        return repo

    def preview_and_initialize(self, runtime, repo):
        request = json.dumps(self.contract()).encode()
        preview = self.run_command(
            [sys.executable, runtime, "v2", "preview", "--contract-file", "-"],
            payload=request,
        )
        self.assertEqual(json.loads(preview.stdout)["status"], "not_initialized")
        self.assertFalse((repo / ".until-loop").exists())
        self.assertEqual(list(self.foreign.iterdir()), [])
        contract_path = self.root / "contract.json"
        contract_path.write_bytes(request)
        self.run_command([
            sys.executable, runtime, "v2", "init", "--repo", repo,
            "--contract-file", contract_path,
        ])
        state = json.loads((repo / ".until-loop" / "state.json").read_text())
        self.assertEqual(state["phase"], "active")
        self.assertEqual(state["repo_root"], str(repo))
        return state

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

    def test_until_loop_preview_and_init_after_isolated_relocation(self):
        package = self.relocated("until-loop")
        runtime = package / "skills/until-loop/scripts/until-loop"
        repo = self.fixture()
        self.preview_and_initialize(runtime, repo)
        self.assertEqual((repo / "notes.txt").read_text(), "Preserve this content.\n")

    def test_improve_binds_and_collects_without_sibling_installation(self):
        package = self.relocated("improve")
        card = package / "skills/improve/SKILL.md"
        self.assertEqual((card.parent / "../../SKILL.md").resolve(), package / "SKILL.md")
        helper = package / "skills/improve/scripts/capture_evidence.py"
        self.assertEqual(helper.parents[3], package)
        self.assertFalse((package.parent / "until-loop").exists())
        repo = self.fixture()
        self.preview_and_initialize(package / "scripts/until-loop", repo)
        result = self.run_command([
            sys.executable, helper, "snapshot", "--repo", repo,
            "--owner", "standalone-improve", "--history-window", "7",
            "--scope", "notes.txt", "--reviewer-identity", "package-fixture",
            "--reviewer-role", "self-review",
        ])
        record_path = Path(result.stdout.decode().strip())
        self.assertEqual(record_path.parent, repo / ".until-loop/evidence")
        record = json.loads(record_path.read_text())
        self.assertEqual(record["tool_facts"]["runtime"]["kind"], "v2")
        self.assertEqual(record["host_claims"]["reviewer"]["role"], "self-review")
        self.assertEqual((repo / "notes.txt").read_text(), "Preserve this content.\n")

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
