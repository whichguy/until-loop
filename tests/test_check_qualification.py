"""Check the qualification harness against known-good and deliberately broken checks."""
import tempfile
import unittest
from pathlib import Path

from qualify_exit_checks import inventory, replay_workspace, verdicts


class QualificationTests(unittest.TestCase):
    def test_invalid_fixture_only_earns_credit_from_a_check_that_passed_every_control(self):
        cases = {"valid": None, "alternate": None, "hold": "distribution-hold",
                 "detected": "wrong-build", "missed": "wrong-build", "ambiguous": "wrong-build"}
        codes = {"valid": (0, 0), "alternate": (0, 1), "hold": (0, 0), "detected": (1, 1),
                 "missed": (0, 0), "ambiguous": (0, 1)}
        rows = {name: [{"id": check_id, "exit_code": rc} for check_id, rc in zip(("build", "literal"), pair)]
                for name, pair in codes.items()}
        self.assertEqual(verdicts(cases, rows),
                         {"valid": "PASS", "alternate": "FALSE_REFUSAL", "hold": "PASS",
                          "detected": "DIFFERENTIATED", "missed": "FALSE_PASS", "ambiguous": "UNRESOLVED"})

    def test_no_checks_or_unusable_commands_cannot_qualify_a_defect(self):
        cases = {"valid": None, "defect": "first-duplicate"}
        self.assertEqual(verdicts(cases, {"valid": [], "defect": []}),
                         {"valid": "UNCOVERED", "defect": "UNCOVERED"})
        for code in (None, -9, 126, 127):
            with self.subTest(code=code):
                rows = {"valid": [{"id": "C1", "exit_code": 0}], "defect": [{"id": "C1", "exit_code": code}]}
                self.assertEqual(verdicts(cases, rows)["defect"], "CHECK_ERROR")

    def test_replay_reserves_original_path_and_only_cleans_its_own_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory).resolve()
            work = run / "trial" / "work"
            capture = {"workspace": str(work)}
            with self.assertRaisesRegex(ValueError, "original evidence directory"):
                with replay_workspace(run, {"workspace": str(run / "other")}):
                    self.fail("entered a relocated workspace")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with replay_workspace(run, capture) as reserved:
                    self.assertEqual(reserved, work)
                    work.mkdir()
                    marker = work / "keep"
                    marker.touch()
                    with self.assertRaises(FileExistsError):
                        with replay_workspace(run, capture):
                            self.fail("entered an existing trial")
                    self.assertTrue(marker.exists())
                    raise RuntimeError("interrupted")
            self.assertFalse(work.parent.exists())

    def test_inventory_rejects_a_link_before_reading_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to(root / "absent")
            with self.assertRaisesRegex(ValueError, "unexpected file type"):
                inventory(root)
