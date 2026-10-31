"""Evaluation harness for benchmarking RepoGuard against labeled historical issues.

Implements the evaluation requirements:
- Benchmark against closed issues labeled 'invalid', 'cannot reproduce', and 'confirmed'.
- Never leaks ground-truth label to the analysis code.
- Reports accuracy, 3x3 confusion matrix, false confirm rate, false flag rate,
  abstention rate, accuracy when not abstaining, and Wilson score confidence intervals.
- Benchmarks against 3 baselines: Always Abstain, Random, and Keyword Baseline.
- Distinguishes development set from held-out test set (reported as headline).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repoguard.models import (
    Claim,
    ClaimVerdict,
    ItemResult,
    ItemType,
    OverallAssessment,
)
from repoguard.analysis.extractor import ClaimExtractor
from repoguard.analysis.verifier import ClaimVerifier


def wilson_score_interval(successes: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Calculate the Wilson score confidence interval (default 95% confidence)."""
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + (z ** 2) / total
    centre = (p + (z ** 2) / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + (z ** 2) / (4 * total)) / total) / denom
    lower = max(0.0, centre - spread)
    upper = min(1.0, centre + spread)
    return round(lower, 3), round(upper, 3)


class MockEvaluationClient:
    """Mock GitHub client populated with frozen repository snapshots."""

    def __init__(self, tree: List[Dict[str, str]], files: Dict[str, str]):
        self._tree = tree
        self._files = files

    def get_repo_tree(self, owner: str, repo: str, ref: str = "HEAD", recursive: bool = True):
        return self._tree

    def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> Optional[str]:
        return self._files.get(path.lstrip("/"))

    def get_pr_files(self, owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
        return []

    def get_tags(self, owner: str, repo: str):
        return []

    def get_releases(self, owner: str, repo: str):
        return []


@dataclass
class BaselineMetrics:
    """Performance metrics for a comparative baseline."""
    name: str
    accuracy: float
    false_confirm_rate: float
    false_flag_rate: float
    abstention_rate: float


@dataclass
class EvaluationMetrics:
    """Aggregated evaluation metrics across benchmark cases."""
    split: str = "test"
    total_cases: int = 0
    correct_matches: int = 0
    accuracy_overall: float = 0.0
    accuracy_ci: Tuple[float, float] = (0.0, 0.0)

    # Costly error rates
    false_confirm_count: int = 0  # Invalid predicted as No strong concern (costliest)
    false_confirm_rate: float = 0.0
    false_flag_count: int = 0     # Confirmed predicted as Review recommended
    false_flag_rate: float = 0.0

    # Abstention metrics
    abstentions: int = 0          # Predicted Insufficient evidence
    abstention_rate: float = 0.0
    accuracy_non_abstaining: float = 0.0

    # Per-label metrics: {label: {"precision": float, "recall": float, "ci_precision": tuple, "ci_recall": tuple}}
    per_label_metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # 3x3 Confusion matrix [expected][predicted]
    confusion_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)
    case_results: List[Dict[str, Any]] = field(default_factory=list)

    # Baselines
    baselines: List[BaselineMetrics] = field(default_factory=list)
    is_synthetic: bool = True

    @property
    def false_positives(self) -> int:
        """Alias for false_confirm_count for backwards compatibility."""
        return self.false_confirm_count

    @property
    def false_negatives(self) -> int:
        """Alias for false_flag_count for backwards compatibility."""
        return self.false_flag_count

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvaluationHarness:
    """Runs RepoGuard evaluation against labeled benchmark issues."""

    def __init__(self, engine: Any = None, dataset_path: Optional[str] = None):
        self.engine = engine
        self.extractor = ClaimExtractor()
        if dataset_path:
            self.dataset_path = Path(dataset_path)
        else:
            # Check for synthetic suite dataset first, fallback to sample
            bench_path = (
                Path(__file__).resolve().parent.parent.parent
                / "evaluation"
                / "data"
                / "synthetic_suite.json"
            )
            if bench_path.exists():
                self.dataset_path = bench_path
            else:
                self.dataset_path = (
                    Path(__file__).resolve().parent.parent.parent
                    / "evaluation"
                    / "data"
                    / "labeled_sample.json"
                )

    def load_dataset(self, split: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load benchmark dataset from JSON, optionally filtering by split."""
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Evaluation dataset not found at {self.dataset_path}")
        with open(self.dataset_path, "r", encoding="utf-8") as f:
            cases = json.load(f)
        if split and split != "all":
            cases = [c for c in cases if c.get("split") == split]
        return cases

    def evaluate_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate an individual labeled case without leaking the label."""
        repo = case["repository"]
        owner, repo_name = repo.split("/")
        body = case.get("body", "")
        title = case.get("title", "")
        ground_truth = case["ground_truth_label"]
        expected = case["expected_assessment"]

        mock_ctx = case.get("mock_repo_context", {})
        client = MockEvaluationClient(
            tree=mock_ctx.get("tree", []),
            files=mock_ctx.get("files", {}),
        )

        # 1. Extract claims
        claims = self.extractor.extract_claims(body=f"{title}\n\n{body}")

        # 2. Verify claims against repo context
        verifier = ClaimVerifier(client, owner, repo_name)
        verified_claims = verifier.verify_claims(claims)

        # 3. Assess item
        item = ItemResult(
            item_type=ItemType.ISSUE,
            number=case.get("number", 0),
            title=title,
            author="reporter",
            url=f"https://github.com/{repo}/issues/{case.get('number', 0)}",
            claims=verified_claims,
        )

        if self.engine and hasattr(self.engine, "_assess_item"):
            self.engine._assess_item(item)
        else:
            self._default_assess_item(item)

        predicted = item.assessment.value

        is_correct = (predicted == expected)
        is_abstention = (predicted == OverallAssessment.INSUFFICIENT_EVIDENCE.value)

        # Costliest error: invalid issue classified as No strong concern found
        is_fc = (
            ground_truth == "invalid"
            and predicted == OverallAssessment.NO_STRONG_CONCERN.value
        )

        # False flag: confirmed issue classified as Review recommended
        is_ff = (
            ground_truth == "confirmed"
            and predicted == OverallAssessment.REVIEW_RECOMMENDED.value
        )

        return {
            "id": case.get("id"),
            "repository": repo,
            "number": case.get("number"),
            "ground_truth": ground_truth,
            "expected_assessment": expected,
            "predicted_assessment": predicted,
            "confidence": item.assessment_confidence.value,
            "reason": item.assessment_reason,
            "claims_count": len(verified_claims),
            "is_correct": is_correct,
            "is_abstention": is_abstention,
            "is_false_confirm": is_fc,
            "is_false_flag": is_ff,
        }

    def _default_assess_item(self, item: ItemResult):
        """Standard assessment roll-up matching engine logic."""
        if not item.claims:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = item.assessment_confidence.LOW
            item.assessment_reason = "No verifiable claims could be extracted"
            return

        verdicts = [c.verdict for c in item.claims]
        supported = sum(1 for v in verdicts if v in (ClaimVerdict.SUPPORTED, ClaimVerdict.REPRODUCED))
        contradicted = sum(1 for v in verdicts if v == ClaimVerdict.CONTRADICTED)
        unverifiable = sum(1 for v in verdicts if v in (ClaimVerdict.UNVERIFIABLE, ClaimVerdict.MISSING_EVIDENCE))
        total = len(verdicts)

        if contradicted > 0:
            item.assessment = OverallAssessment.REVIEW_RECOMMENDED
            item.assessment_confidence = (
                item.assessment_confidence.HIGH if contradicted / total > 0.5
                else item.assessment_confidence.MEDIUM if contradicted / total > 0.25
                else item.assessment_confidence.LOW
            )
            item.assessment_reason = f"{contradicted} of {total} claims contradicted by evidence"
        elif unverifiable / total > 0.7:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = item.assessment_confidence.MEDIUM
            item.assessment_reason = f"{unverifiable} of {total} claims could not be verified"
        elif supported / total > 0.5:
            item.assessment = OverallAssessment.NO_STRONG_CONCERN
            item.assessment_confidence = (
                item.assessment_confidence.HIGH if supported / total > 0.8
                else item.assessment_confidence.MEDIUM
            )
            item.assessment_reason = f"{supported} of {total} claims supported by evidence"
        else:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = item.assessment_confidence.LOW
            item.assessment_reason = "Mixed or inconclusive evidence"

    def run(self, split: str = "test") -> EvaluationMetrics:
        """Execute evaluation across the dataset and compute comprehensive metrics."""
        cases = self.load_dataset(split=split)
        is_synthetic = any(c.get("synthetic", False) for c in cases) if cases else ("synthetic" in str(self.dataset_path))
        metrics = EvaluationMetrics(split=split, total_cases=len(cases), is_synthetic=is_synthetic)

        labels = [
            OverallAssessment.NO_STRONG_CONCERN.value,
            OverallAssessment.REVIEW_RECOMMENDED.value,
            OverallAssessment.INSUFFICIENT_EVIDENCE.value,
        ]
        metrics.confusion_matrix = {exp: {pred: 0 for pred in labels} for exp in labels}

        total_invalid = 0
        total_confirmed = 0

        for case in cases:
            res = self.evaluate_case(case)
            metrics.case_results.append(res)

            exp = res["expected_assessment"]
            pred = res["predicted_assessment"]
            gt = res["ground_truth"]

            if gt == "invalid":
                total_invalid += 1
            elif gt == "confirmed":
                total_confirmed += 1

            if exp in metrics.confusion_matrix and pred in metrics.confusion_matrix[exp]:
                metrics.confusion_matrix[exp][pred] += 1

            if res["is_correct"]:
                metrics.correct_matches += 1
            if res["is_abstention"]:
                metrics.abstentions += 1
            if res["is_false_confirm"]:
                metrics.false_confirm_count += 1
            if res["is_false_flag"]:
                metrics.false_flag_count += 1

        if metrics.total_cases > 0:
            metrics.accuracy_overall = round(metrics.correct_matches / metrics.total_cases, 3)
            metrics.accuracy_ci = wilson_score_interval(metrics.correct_matches, metrics.total_cases)
            metrics.abstention_rate = round(metrics.abstentions / metrics.total_cases, 3)

        if total_invalid > 0:
            metrics.false_confirm_rate = round(metrics.false_confirm_count / total_invalid, 3)
        if total_confirmed > 0:
            metrics.false_flag_rate = round(metrics.false_flag_count / total_confirmed, 3)

        non_abstained = metrics.total_cases - metrics.abstentions
        if non_abstained > 0:
            correct_non_abstained = sum(
                1 for r in metrics.case_results if r["is_correct"] and not r["is_abstention"]
            )
            metrics.accuracy_non_abstaining = round(correct_non_abstained / non_abstained, 3)

        # Compute per-label precision and recall with Wilson score intervals
        for lbl in labels:
            tp = metrics.confusion_matrix[lbl][lbl]
            actual_total = sum(metrics.confusion_matrix[lbl].values())
            predicted_total = sum(metrics.confusion_matrix[row_lbl][lbl] for row_lbl in labels)

            prec = round(tp / predicted_total, 3) if predicted_total > 0 else 0.0
            rec = round(tp / actual_total, 3) if actual_total > 0 else 0.0

            prec_ci = wilson_score_interval(tp, predicted_total) if predicted_total > 0 else (0.0, 0.0)
            rec_ci = wilson_score_interval(tp, actual_total) if actual_total > 0 else (0.0, 0.0)

            metrics.per_label_metrics[lbl] = {
                "precision": prec,
                "recall": rec,
                "precision_ci": prec_ci,
                "recall_ci": rec_ci,
            }

        # Run Baselines
        metrics.baselines = self._compute_baselines(cases)

        return metrics

    def _compute_baselines(self, cases: List[Dict[str, Any]]) -> List[BaselineMetrics]:
        """Compute comparison baselines on the same cases."""
        n = len(cases)
        if n == 0:
            return []

        # 1. Always Abstain ("Insufficient evidence")
        abstain_correct = sum(
            1 for c in cases if c["expected_assessment"] == OverallAssessment.INSUFFICIENT_EVIDENCE.value
        )
        b_abstain = BaselineMetrics(
            name="Always Abstain",
            accuracy=round(abstain_correct / n, 3),
            false_confirm_rate=0.0,
            false_flag_rate=0.0,
            abstention_rate=1.0,
        )

        # 2. Random Baseline
        rng = random.Random(42)
        choices = [
            OverallAssessment.NO_STRONG_CONCERN.value,
            OverallAssessment.REVIEW_RECOMMENDED.value,
            OverallAssessment.INSUFFICIENT_EVIDENCE.value,
        ]
        rnd_correct = 0
        rnd_fc = 0
        rnd_ff = 0
        rnd_abs = 0
        inv_count = sum(1 for c in cases if c["ground_truth_label"] == "invalid")
        conf_count = sum(1 for c in cases if c["ground_truth_label"] == "confirmed")

        for c in cases:
            pred = rng.choice(choices)
            if pred == c["expected_assessment"]:
                rnd_correct += 1
            if pred == OverallAssessment.INSUFFICIENT_EVIDENCE.value:
                rnd_abs += 1
            if c["ground_truth_label"] == "invalid" and pred == OverallAssessment.NO_STRONG_CONCERN.value:
                rnd_fc += 1
            if c["ground_truth_label"] == "confirmed" and pred == OverallAssessment.REVIEW_RECOMMENDED.value:
                rnd_ff += 1

        b_random = BaselineMetrics(
            name="Random (1/3)",
            accuracy=round(rnd_correct / n, 3),
            false_confirm_rate=round(rnd_fc / inv_count, 3) if inv_count else 0.0,
            false_flag_rate=round(rnd_ff / conf_count, 3) if conf_count else 0.0,
            abstention_rate=round(rnd_abs / n, 3),
        )

        # 3. Keyword Baseline (if text mentions 'error' or 'bug' -> Review recommended, else No strong concern)
        kw_correct = 0
        kw_fc = 0
        kw_ff = 0
        kw_abs = 0
        for c in cases:
            text = (c.get("title", "") + " " + c.get("body", "")).lower()
            if any(k in text for k in ("error", "crash", "bug", "segfault", "overflow", "missing")):
                pred = OverallAssessment.REVIEW_RECOMMENDED.value
            else:
                pred = OverallAssessment.NO_STRONG_CONCERN.value

            if pred == c["expected_assessment"]:
                kw_correct += 1
            if c["ground_truth_label"] == "invalid" and pred == OverallAssessment.NO_STRONG_CONCERN.value:
                kw_fc += 1
            if c["ground_truth_label"] == "confirmed" and pred == OverallAssessment.REVIEW_RECOMMENDED.value:
                kw_ff += 1

        b_kw = BaselineMetrics(
            name="Keyword Heuristic",
            accuracy=round(kw_correct / n, 3),
            false_confirm_rate=round(kw_fc / inv_count, 3) if inv_count else 0.0,
            false_flag_rate=round(kw_ff / conf_count, 3) if conf_count else 0.0,
            abstention_rate=0.0,
        )

        return [b_abstain, b_random, b_kw]

    def format_summary(self, metrics: EvaluationMetrics) -> str:
        """Format evaluation metrics as an informative terminal table."""
        lines = []
        lines.append("=" * 76)
        if getattr(metrics, "is_synthetic", True):
            lines.append(f"Synthetic Regression Suite — Split: [{metrics.split.upper()} SET]")
            lines.append("Synthetic cases; not a measure of accuracy on real GitHub issues.")
        else:
            lines.append(f"RepoGuard Evaluation Benchmark Results — Split: [{metrics.split.upper()} SET]")
        lines.append("=" * 76)
        lines.append(f"Total Test Cases:          {metrics.total_cases}")
        ci_str = f"[{metrics.accuracy_ci[0]*100:.1f}%, {metrics.accuracy_ci[1]*100:.1f}%]"
        lines.append(f"Overall Accuracy:          {metrics.accuracy_overall * 100:.1f}%  (95% CI: {ci_str})")
        lines.append(f"Abstention Rate:           {metrics.abstention_rate * 100:.1f}%  ({metrics.abstentions}/{metrics.total_cases})")
        lines.append(f"Accuracy on Non-Abstained: {metrics.accuracy_non_abstaining * 100:.1f}%")
        lines.append("-" * 76)
        lines.append("Risk & Error Metrics (Cost Analysis):")
        lines.append(f"  False Confirm Rate (costliest): {metrics.false_confirm_rate * 100:.1f}%  (invalid waved through as valid)")
        lines.append(f"  False Flag Rate:                {metrics.false_flag_rate * 100:.1f}%  (confirmed flagged as review recommended)")
        lines.append("-" * 76)
        lines.append("Per-Label Precision & Recall (with 95% Wilson CIs):")
        short_names = {
            OverallAssessment.NO_STRONG_CONCERN.value: "No Concern",
            OverallAssessment.REVIEW_RECOMMENDED.value: "Review Rec",
            OverallAssessment.INSUFFICIENT_EVIDENCE.value: "Insuff Ev",
        }
        lines.append(f"{'Label':<12} | {'Precision (95% CI)':<28} | {'Recall (95% CI)':<28}")
        lines.append("-" * 76)
        for lbl, data in metrics.per_label_metrics.items():
            name = short_names.get(lbl, lbl)
            p_str = f"{data['precision']*100:4.1f}% [{data['precision_ci'][0]*100:.0f}%, {data['precision_ci'][1]*100:.0f}%]"
            r_str = f"{data['recall']*100:4.1f}% [{data['recall_ci'][0]*100:.0f}%, {data['recall_ci'][1]*100:.0f}%]"
            lines.append(f"{name:<12} | {p_str:<28} | {r_str:<28}")

        lines.append("-" * 76)
        lines.append("Confusion Matrix (Rows: Ground Truth Expected, Cols: Predicted):")
        headers = ["Expected \\ Predicted", "No Concern", "Review Rec", "Insuff Ev"]
        lines.append(f"{headers[0]:<25} | {headers[1]:<12} | {headers[2]:<12} | {headers[3]:<12}")
        lines.append("-" * 76)

        for exp_key, row in metrics.confusion_matrix.items():
            r_name = short_names.get(exp_key, exp_key)
            c1 = row.get(OverallAssessment.NO_STRONG_CONCERN.value, 0)
            c2 = row.get(OverallAssessment.REVIEW_RECOMMENDED.value, 0)
            c3 = row.get(OverallAssessment.INSUFFICIENT_EVIDENCE.value, 0)
            lines.append(f"{r_name:<25} | {c1:<12} | {c2:<12} | {c3:<12}")

        lines.append("-" * 76)
        lines.append("Baseline Comparisons on Held-Out Test Set:")
        lines.append(f"{'Model / Method':<22} | {'Accuracy':<10} | {'False Confirm':<15} | {'False Flag':<12} | {'Abstain':<10}")
        lines.append("-" * 76)
        lines.append(
            f"{'RepoGuard':<22} | {metrics.accuracy_overall*100:5.1f}%   | {metrics.false_confirm_rate*100:5.1f}%         | {metrics.false_flag_rate*100:5.1f}%       | {metrics.abstention_rate*100:5.1f}%"
        )
        for b in metrics.baselines:
            lines.append(
                f"{b.name:<22} | {b.accuracy*100:5.1f}%   | {b.false_confirm_rate*100:5.1f}%         | {b.false_flag_rate*100:5.1f}%       | {b.abstention_rate*100:5.1f}%"
            )

        lines.append("=" * 76)
        return "\n".join(lines)
