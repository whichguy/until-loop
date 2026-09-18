import unittest

from stable_unique import stable_unique


class StableUniqueTests(unittest.TestCase):
    def test_identical_lowercase_strings(self):
        self.assertEqual(stable_unique(["a", "a"]), ["a"])


if __name__ == "__main__":
    unittest.main()
