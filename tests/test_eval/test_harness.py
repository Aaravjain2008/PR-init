"""Tests for the Evaluation Harness."""

import unittest
from repoguard.evaluation.harness import EvaluationHarness, EvaluationMetrics


class TestEvaluationHarness(unittest.TestCase):
    """Test evaluation harness execution and metric calculations."""

    def setUp(self):
        self.harness = EvaluationHarness()

    def test_load_dataset(self):
        cases = self.harness.load_dataset()
        self.assertTrue(len(cases) >= 5)
        # Check structure of case 0
        c0 = cases[0]
        self.assertIn("repository", c0)
        self.assertIn("ground_truth_label", c0)
        self.assertIn("expected_assessment", c0)
        self.assertTrue(c0.get("synthetic"))
        self.assertEqual(c0.get("selection_method"), "Synthetic case written by the project team")

    def test_run_benchmark(self):
        metrics = self.harness.run()
        self.assertIsInstance(metrics, EvaluationMetrics)
        self.assertGreater(metrics.total_cases, 0)
        self.assertTrue(metrics.is_synthetic)
        # No false positives or negatives should occur on our reference cases
        self.assertEqual(metrics.false_positives, 0)
        self.assertEqual(metrics.false_negatives, 0)
        # High accuracy on benchmark
        self.assertGreaterEqual(metrics.accuracy_overall, 0.8)

    def test_format_summary(self):
        metrics = self.harness.run()
        summary = self.harness.format_summary(metrics)
        self.assertIn("Synthetic Regression Suite", summary)
        self.assertIn("Synthetic cases; not a measure of accuracy on real GitHub issues.", summary)
        self.assertIn("Confusion Matrix", summary)


if __name__ == "__main__":
    unittest.main()

