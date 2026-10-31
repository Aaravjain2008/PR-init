"""Tests for secondary signals analyzer."""

import unittest
from repoguard.analysis.signals import SignalAnalyzer, DuplicateSignal, ContributorSignal


class TestSignalAnalyzer(unittest.TestCase):
    """Test neutral duplicate detection and contributor metrics."""

    def test_text_similarity(self):
        a = "Fix buffer overflow in parser.c"
        b = "fix buffer overflow in parser.c"
        self.assertEqual(SignalAnalyzer.compute_text_similarity(a, b), 1.0)

        c = "Add documentation for feature X"
        sim = SignalAnalyzer.compute_text_similarity(a, c)
        self.assertTrue(sim < 0.4)

    def test_near_duplicate_detection(self):
        curr = {
            "number": 105,
            "title": "Segmentation fault when input string is empty in parse_json",
            "body": "When passing an empty string to parse_json(), the application crashes with a segfault.",
        }
        history = [
            {
                "number": 98,
                "title": "Segmentation fault when input string is empty in parse_json()",
                "body": "When passing an empty string to parse_json(), the program crashes with a segfault.",
            },
            {
                "number": 40,
                "title": "Update copyright year in README",
                "body": "Bump copyright year to 2026.",
            }
        ]

        dups = SignalAnalyzer.find_near_duplicates(curr, history, threshold=0.8)
        self.assertEqual(len(dups), 2)  # Title and body match #98
        self.assertEqual(dups[0].matched_number, 98)
        self.assertIn("similarity to #98", dups[0].description)

    def test_contributor_signal_neutrality(self):
        signal = ContributorSignal(
            author_login="octocat",
            account_created_at="2020-01-01T00:00:00Z",
            public_repo_count=5,
            has_prior_contributions_in_repo=False,
            note="First contribution in this repository",
        )
        d = signal.to_dict()
        # Verify no accusatory language is stored
        serialized = str(d).lower()
        self.assertNotIn("spam", serialized)
        self.assertNotIn("fake", serialized)
        self.assertNotIn("malicious", serialized)
        self.assertNotIn("sybil", serialized)


if __name__ == "__main__":
    unittest.main()

