"""Secondary signals: duplicate detection and contributor activity patterns.

Per RepoGuard design principles:
- Contributor-level and duplicate-submission analysis is a SECONDARY SIGNAL.
- It supports triage but NEVER replaces claim verification.
- It NEVER labels a person as 'spam', 'fake', 'malicious', or 'Sybil'.
- It reports neutral, factual metrics (e.g. text similarity, account age).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class DuplicateSignal:
    """Neutral comparison signal indicating similarity with previous items."""
    target_number: int
    matched_number: int
    similarity_ratio: float  # 0.0 to 1.0
    matched_type: str  # "title", "body", "diff"
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContributorSignal:
    """Neutral factual summary of contributor context."""
    author_login: str
    account_created_at: Optional[str] = None
    public_repo_count: Optional[int] = None
    has_prior_contributions_in_repo: Optional[bool] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SignalAnalyzer:
    """Analyzes secondary context signals neutrally."""

    @staticmethod
    def compute_text_similarity(a: str, b: str) -> float:
        """Compute normalized similarity ratio between two text strings."""
        if not a or not b:
            return 0.0
        # Normalize whitespace and lowercase
        norm_a = " ".join(a.lower().split())
        norm_b = " ".join(b.lower().split())
        matcher = difflib.SequenceMatcher(None, norm_a, norm_b)
        return round(matcher.ratio(), 3)

    @classmethod
    def find_near_duplicates(
        cls,
        current_item: Dict[str, Any],
        historical_items: List[Dict[str, Any]],
        threshold: float = 0.75,
    ) -> List[DuplicateSignal]:
        """Check if an issue or PR is a near-duplicate of prior submissions.
        
        Args:
            current_item: Dict with 'number', 'title', 'body'
            historical_items: List of item dicts
            threshold: Minimum similarity ratio to flag
            
        Returns:
            List of neutral DuplicateSignal findings
        """
        duplicates: List[DuplicateSignal] = []
        curr_num = current_item.get("number", 0)
        curr_title = current_item.get("title", "")
        curr_body = current_item.get("body", "") or ""

        for prev in historical_items:
            prev_num = prev.get("number", 0)
            if prev_num == curr_num:
                continue

            prev_title = prev.get("title", "")
            prev_body = prev.get("body", "") or ""

            # 1. Compare titles
            title_sim = cls.compute_text_similarity(curr_title, prev_title)
            if title_sim >= threshold and len(curr_title) > 10:
                duplicates.append(DuplicateSignal(
                    target_number=curr_num,
                    matched_number=prev_num,
                    similarity_ratio=title_sim,
                    matched_type="title",
                    description=(
                        f"Title has {int(title_sim * 100)}% similarity to #{prev_num}"
                    ),
                ))

            # 2. Compare bodies (only if non-trivial length)
            if len(curr_body) > 50 and len(prev_body) > 50:
                body_sim = cls.compute_text_similarity(curr_body, prev_body)
                if body_sim >= threshold:
                    duplicates.append(DuplicateSignal(
                        target_number=curr_num,
                        matched_number=prev_num,
                        similarity_ratio=body_sim,
                        matched_type="body",
                        description=(
                            f"Description body has {int(body_sim * 100)}% "
                            f"similarity to #{prev_num}"
                        ),
                    ))

        return duplicates

