"""Run the standalone relocated-plugin E2E harness inside unittest discovery."""
import unittest

import harness_e2e


class RelocatedE2EHarnessTests(unittest.TestCase):
    def test_relocated_plugin_e2e_harness(self):
        summary = harness_e2e.run_all()
        self.assertTrue(summary["passed"], summary)
        self.assertEqual(len(summary["scenarios"]), len(harness_e2e.SCENARIOS))
        self.assertTrue(all(item["passed"] for item in summary["scenarios"]), summary)


if __name__ == "__main__":
    unittest.main()
