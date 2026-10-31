"""Evaluation harness for benchmarking RepoGuard against labeled historical issues.

Implements the evaluation scenario from spec §2 & §13:
- Tests against closed issues labeled 'invalid', 'cannot reproduce', and 'confirmed'.
- Reports accuracy, confusion matrix, false positives, false negatives, and abstentions.
- Explicitly measures the policy preference: abstaining ('Insufficient evidence')
  over confident wrong conclusions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from repoguard.models import (
    Claim,
    ClaimVerdict,
    ItemResult,
    ItemType,
    OverallAssessment,
    ScanResult,
)
from repoguard.analysis.extractor import ClaimExtractor
from repoguard.analysis.verifier import ClaimVerifier


class MockEvaluationClient:
    """Mock GitHub client populated with frozen repository snapshots."""

    def __init__(self, tree: List[Dict[str, str]], files: Dict[str, str]):
        self._tree = tree
        self._files = files

    def get_repo_tree(self, owner: str, repo: str, ref: str = "HEAD", recursive: bool = True):
        return self._tree

    def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> Optional[str]:
        # Handle leading slashes or variations
        return self._files.get(path.lstrip("/"))

    def get_pr_files(self, owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
        return []


@dataclass
class EvaluationMetrics:
    """Aggregated evaluation metrics across benchmark cases."""
    total_cases: int = 0
    correct_matches: int = 0
    abstentions: int = 0  # System said "Insufficient evidence"
    false_positives: int = 0  # Invalid issue classified as No strong concern
    false_negatives: int = 0  # Confirmed issue classified as Review recommended
    accuracy_overall: float = 0.0
    accuracy_effective: float = 0.0  # Accuracy excluding abstentions
    confusion_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)
    case_results: List[Dict[str, Any]] = field(default_factory=list)

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
            self.dataset_path = (
                Path(__file__).resolve().parent.parent.parent
                / "evaluation"
                / "data"
                / "labeled_sample.json"
            )

    def load_dataset(self) -> List[Dict[str, Any]]:
        """Load benchmark dataset from JSON."""
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Evaluation dataset not found at {self.dataset_path}")
        with open(self.dataset_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def evaluate_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate an individual labeled case."""
        repo = case["repository"]
        owner, repo_name = repo.split("/")
        body = case.get("body", "")
        title = case.get("title", "")
        ground_truth = case["ground_truth_label"]
        expected = case["expected_assessment"]

        # Setup mock client using snapshot if present
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
            author="contributor",
            url=f"https://github.com/{repo}/issues/{case.get('number', 0)}",
            claims=verified_claims,
        )

        # Use engine assessment logic if available, or internal rule
        if self.engine and hasattr(self.engine, "_assess_item"):
            self.engine._assess_item(item)
        else:
            self._default_assess_item(item)

        predicted = item.assessment.value

        is_correct = (predicted == expected)
        is_abstention = (predicted == OverallAssessment.INSUFFICIENT_EVIDENCE.value)

        # False positive: ground truth was invalid/cannot reproduce, but classified as No strong concern
        is_fp = (
            ground_truth in ("invalid", "cannot reproduce")
            and predicted == OverallAssessment.NO_STRONG_CONCERN.value
        )

        # False negative: ground truth was confirmed, but classified as Review recommended
        is_fn = (
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
            "is_false_positive": is_fp,
            "is_false_negative": is_fn,
        }

    def _default_assess_item(self, item: ItemResult):
        """Standard assessment roll-up matching engine logic."""
        if not item.claims:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_reason = "No verifiable claims could be extracted"
            return

        verdicts = [c.verdict for c in item.claims]
        supported = sum(1 for v in verdicts if v in (ClaimVerdict.SUPPORTED, ClaimVerdict.REPRODUCED))
        contradicted = sum(1 for v in verdicts if v == ClaimVerdict.CONTRADICTED)
        unverifiable = sum(1 for v in verdicts if v in (ClaimVerdict.UNVERIFIABLE, ClaimVerdict.MISSING_EVIDENCE))
        total = len(verdicts)

        if contradicted > 0:
            item.assessment = OverallAssessment.REVIEW_RECOMMENDED
            item.assessment_reason = f"{contradicted} of {total} claims contradicted by evidence"
        elif unverifiable / total > 0.7:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_reason = f"{unverifiable} of {total} claims could not be verified"
        elif supported / total > 0.5:
            item.assessment = OverallAssessment.NO_STRONG_CONCERN
            item.assessment_reason = f"{supported} of {total} claims supported by evidence"
        else:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_reason = "Mixed or inconclusive evidence"

    def run(self) -> EvaluationMetrics:
        """Execute evaluation across the full dataset and compute summary metrics."""
        cases = self.load_dataset()
        metrics = EvaluationMetrics(total_cases=len(cases))

        # Initialize confusion matrix [expected][predicted]
        labels = [
            OverallAssessment.NO_STRONG_CONCERN.value,
            OverallAssessment.REVIEW_RECOMMENDED.value,
            OverallAssessment.INSUFFICIENT_EVIDENCE.value,
        ]
        metrics.confusion_matrix = {exp: {pred: 0 for pred in labels} for exp in labels}

        for case in cases:
            res = self.evaluate_case(case)
            metrics.case_results.append(res)

            exp = res["expected_assessment"]
            pred = res["predicted_assessment"]
            if exp in metrics.confusion_matrix and pred in metrics.confusion_matrix[exp]:
                metrics.confusion_matrix[exp][pred] += 1

            if res["is_correct"]:
                metrics.correct_matches += 1
            if res["is_abstention"]:
                metrics.abstentions += 1
            if res["is_false_positive"]:
                metrics.false_positives += 1
            if res["is_false_negative"]:
                metrics.false_negatives += 1

        if metrics.total_cases > 0:
            metrics.accuracy_overall = round(metrics.correct_matches / metrics.total_cases, 3)

        decisive_cases = metrics.total_cases - metrics.abstentions
        if decisive_cases > 0:
            metrics.accuracy_effective = round(
                (metrics.correct_matches - (metrics.abstentions if metrics.abstentions > 0 else 0))
                / decisive_cases,
                3,
            )

        return metrics

    def format_summary(self, metrics: EvaluationMetrics) -> str:
        """Format evaluation metrics as an informative terminal table."""
        lines = []
        lines.append("=" * 72)
        lines.append("RepoGuard Evaluation Benchmark Results")
        lines.append("=" * 72)
        lines.append(f"Total Test Cases:       {metrics.total_cases}")
        lines.append(f"Correct Assessments:    {metrics.correct_matches} ({int(metrics.accuracy_overall * 100)}%)")
        lines.append(f"Abstentions (Uncertain):{metrics.abstentions}")
        lines.append(f"False Positives:        {metrics.false_positives} (Invalid classified as valid)")
        lines.append(f"False Negatives:        {metrics.false_negatives} (Valid classified as contradicted)")
        lines.append("-" * 72)
        lines.append("Policy Observation:")
        lines.append("  RepoGuard favors explicit uncertainty ('Insufficient evidence')")
        lines.append("  over making unsupported conclusions.")
        lines.append("-" * 72)
        lines.append("Confusion Matrix (Rows: Expected, Cols: Predicted):")
        headers = ["Expected \\ Predicted", "No Concern", "Review Rec", "Insuff Ev"]
        lines.append(f"{headers[0]:<25} | {headers[1]:<12} | {headers[2]:<12} | {headers[3]:<12}")
        lines.append("-" * 72)

        short_names = {
            OverallAssessment.NO_STRONG_CONCERN.value: "No Concern",
            OverallAssessment.REVIEW_RECOMMENDED.value: "Review Rec",
            OverallAssessment.INSUFFICIENT_EVIDENCE.value: "Insuff Ev",
        }
        for exp_key, row in metrics.confusion_matrix.items():
            r_name = short_names.get(exp_key, exp_key)
            c1 = row.get(OverallAssessment.NO_STRONG_CONCERN.value, 0)
            c2 = row.get(OverallAssessment.REVIEW_RECOMMENDED.value, 0)
            c3 = row.get(OverallAssessment.INSUFFICIENT_EVIDENCE.value, 0)
            lines.append(f"{r_name:<25} | {c1:<12} | {c2:<12} | {c3:<12}")

        lines.append("=" * 72)
        return "\n".join(lines)

