import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from acceptance_cases import CASES, REQUESTS, STOP_REASONS, render


class AcceptanceCaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fixture(self, family, case):
        workspace = self.root / case
        workspace.mkdir()
        render(workspace, family, case)
        return workspace

    def run_contact_code(self, workspace, code):
        return subprocess.run(
            [sys.executable, "-E", "-B", "-c", "from contacts import import_contacts\n" + code],
            cwd=workspace, capture_output=True, text=True, check=False,
        )

    def test_contacts_catalog_matches_importer_and_readme(self):
        self.assertIn("raise ValueError", REQUESTS["contacts"])
        for case, expected in CASES["contacts"].items():
            with self.subTest(case=case):
                workspace = self.fixture("contacts", case)
                result = self.run_contact_code(
                    workspace, 'import json\nprint(json.dumps(import_contacts("contacts.csv")))',
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = json.loads(result.stdout)
                self.assertEqual(len(rows), 2)
                contacts = {row["email"]: row for row in rows}
                self.assertEqual(set(contacts), {"alice@example.com", "bob@example.com"})
                self.assertEqual(contacts["bob@example.com"]["phone"], "001-555-1111")
                first_contact = contacts["alice@example.com"]
                observed = []
                if first_contact["first_name"] != "Alice":
                    self.assertEqual(first_contact["first_name"], "Alicia")
                    observed.append("first-duplicate")
                original_phones = {"Alice": "  (415) 555-0182  ", "Alicia": "  4155550199  "}
                if first_contact["phone"] != original_phones[first_contact["first_name"]]:
                    observed.append("preserve-phone")

                (workspace / "missing-email.csv").write_text(
                    "first_name,last_name,email\nAda,Lovelace,\n", encoding="utf-8",
                )
                missing = self.run_contact_code(workspace, 'print(import_contacts("missing-email.csv"))')
                if missing.returncode == 0:
                    self.assertEqual(missing.stdout.strip(), "[]")
                    observed.append("reject-missing-email")
                else:
                    self.assertIn("ValueError: row 2 has no email", missing.stderr)

                readme = (workspace / "README.md").read_text(encoding="utf-8")
                if case == "paraphrased":
                    self.assertIn("a different mix of capitals, the earliest row wins", readme)
                    self.assertIn("Telephone text is copied verbatim", readme)
                    self.assertIn("Rows without an email address raise an error", readme)
                else:
                    self.assertIn("two email addresses differ only by case", readme)
                    if "the last contact is kept" in readme:
                        self.assertNotIn("the first contact is kept", readme)
                        observed.append("docs-first-duplicate")
                    else:
                        self.assertIn("the first contact is kept", readme)
                    self.assertIn("The phone string is preserved exactly as supplied", readme)
                    self.assertIn("A row with a missing email is rejected", readme)
                self.assertEqual(observed, [expected] if expected else [])

    def test_contact_duplicate_check_distinguishes_business_failure_from_schema_error(self):
        good = self.fixture("contacts", "valid")
        wrong_winner = self.fixture("contacts", "code-last-wins")
        wrong_docs = self.fixture("contacts", "docs-last-wins")
        duplicate_check = """\
winner, = (row for row in import_contacts("contacts.csv") if row["email"] == "alice@example.com")
assert winner["first_name"] == "Alice", winner
"""
        for workspace in (good, wrong_docs):
            result = self.run_contact_code(workspace, duplicate_check)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_contact_code(wrong_winner, duplicate_check)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError:", result.stderr)
        self.assertIn("'first_name': 'Alicia'", result.stderr)
        for workspace in (good, wrong_winner):
            broken = self.run_contact_code(workspace, duplicate_check.replace('"first_name"', '"name"'))
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("KeyError: 'name'", broken.stderr)
            self.assertNotIn("AssertionError:", broken.stderr)

    def test_release_catalog_matches_artifacts(self):
        self.assertEqual(STOP_REASONS, {"distribution-hold"})
        self.assertIn("distribution-hold.txt", REQUESTS["release"])
        for case, expected in CASES["release"].items():
            with self.subTest(case=case):
                workspace = self.fixture("release", case)
                json_manifest = workspace / "release.json"
                yaml_manifest = workspace / "release.yaml"
                self.assertEqual(yaml_manifest.exists(), case == "yaml-only")
                self.assertNotEqual(json_manifest.exists(), yaml_manifest.exists())
                if json_manifest.exists():
                    manifest = json.loads(json_manifest.read_bytes())
                else:
                    # Check this fixture's small YAML schema without adding a parser dependency.
                    match = re.fullmatch(
                        r"service: (?P<service>[\w-]+)\nversion: (?P<version>[\d.]+)\n"
                        r"target: (?P<target>\w+)\nbuild: (?P<build>[a-f\d]+)\ncanary:\n  enabled: true\n",
                        yaml_manifest.read_text(encoding="utf-8"),
                    )
                    self.assertIsNotNone(match)
                    manifest = {**match.groupdict(), "canary": {"enabled": True}}
                self.assertEqual(manifest["service"], "demo-service")
                self.assertEqual(manifest["version"], "2.4.1")
                self.assertEqual(manifest["target"], "staging")
                self.assertRegex(manifest["build"], r"^[0-9a-f]{7}$")
                enabled = manifest["canary"]["enabled"]
                self.assertIs(enabled, case != "disabled-no-report")

                failures = []
                report = workspace / "canary-report.json"
                self.assertEqual(report.exists(), case not in {"disabled-no-report", "enabled-no-report"})
                if enabled:
                    if not report.exists():
                        failures.append("canary-build")
                    else:
                        report_data = json.loads(report.read_bytes())
                        self.assertEqual(report_data["service"], manifest["service"])
                        self.assertRegex(report_data["build"], r"^[0-9a-f]{7}$")
                        if report_data["build"] != manifest["build"]:
                            failures.append("canary-build")
                scan = json.loads((workspace / "security-scan.json").read_bytes())
                self.assertIs(type(scan["critical"]), int)
                if scan["critical"] != 0:
                    failures.append("zero-critical")

                digest, name = (workspace / "checksums.txt").read_text(encoding="utf-8").split()
                self.assertEqual(name, "bundle.bin")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                if digest != hashlib.sha256((workspace / name).read_bytes()).hexdigest():
                    failures.append("bundle-digest")

                instruction = (workspace / "rollback.md").read_text(encoding="utf-8").split("\n\n")[1].strip()
                if case == "rollback-negated":
                    self.assertTrue(instruction.startswith("Do not restore the previously deployed image by digest."))
                    failures.append("rollback-by-digest")
                elif case == "rollback-omitted":
                    self.assertEqual(instruction, "Contact the duty operator and record the change.")
                    failures.append("rollback-by-digest")
                elif case == "paraphrased-rollback":
                    self.assertIn("Redeploy the image that served traffic before this release", instruction)
                    self.assertIn("selecting it by its immutable SHA-256 identifier", instruction)
                else:
                    self.assertTrue(instruction.startswith("Restore the previously deployed image by digest,"))

                hold = workspace / "distribution-hold.txt"
                if hold.exists():
                    self.assertEqual(failures, [])
                    self.assertEqual(hold.read_text(encoding="utf-8").strip(), "Distribution is on hold.")
                    failures.append("distribution-hold")
                self.assertEqual(failures, [expected] if expected else [])

    def test_release_alternative_build_is_distinct_and_still_matches_report(self):
        primary = self.fixture("release", "valid")
        alternate = self.fixture("release", "different-matching-build")
        primary_build = json.loads((primary / "release.json").read_bytes())["build"]
        alternate_build = json.loads((alternate / "release.json").read_bytes())["build"]
        report_build = json.loads((alternate / "canary-report.json").read_bytes())["build"]
        self.assertNotEqual(alternate_build, primary_build)
        self.assertEqual(alternate_build, report_build)

    def test_render_needs_empty_workspace(self):
        workspace = self.fixture("release", "valid")
        with self.assertRaises(ValueError):
            render(workspace, "release", "disabled-no-report")
        self.assertTrue((workspace / "canary-report.json").is_file())
