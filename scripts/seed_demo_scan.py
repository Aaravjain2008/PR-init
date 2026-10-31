#!/usr/bin/env python3
"""Seed demo scans into local SQLite database for instant dashboard preview."""

import sys
from pathlib import Path

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from repoguard.evaluation.harness import EvaluationHarness
from repoguard.engine import RepoGuardEngine
from repoguard.models import EngineConfig, ScanResult, ScanStatus, ItemResult, ItemType, OverallAssessment, Confidence, Claim, ClaimVerdict, Evidence, EvidenceType, FindingCategory

def seed():
    engine = RepoGuardEngine(EngineConfig(db_path="repoguard.db"))
    harness = EvaluationHarness(engine=engine)
    cases = harness.load_dataset()

    scan = ScanResult(
        repository="curl/curl",
        scope="open issues & pull requests (triage queue)",
        scan_status=ScanStatus.COMPLETE,
        items_found=len(cases),
        items_scanned=len(cases),
    )

    for case in cases:
        res = harness.evaluate_case(case)
        mock_ctx = case.get("mock_repo_context", {})
        claims = harness.extractor.extract_claims(f"{case['title']}\n\n{case.get('body', '')}")
        from repoguard.analysis.verifier import ClaimVerifier
        from repoguard.evaluation.harness import MockEvaluationClient
        client = MockEvaluationClient(mock_ctx.get("tree", []), mock_ctx.get("files", {}))
        verifier = ClaimVerifier(client, "curl", "curl")
        verified_claims = verifier.verify_claims(claims)

        item = ItemResult(
            item_type=ItemType.ISSUE,
            number=case.get("number", 0),
            title=case.get("title", ""),
            author="contributor_" + str(case.get("number", 0)),
            url=f"https://github.com/{case['repository']}/issues/{case.get('number', 0)}",
            claims=verified_claims,
            assessment=OverallAssessment(res["predicted_assessment"]),
            assessment_confidence=Confidence(res["confidence"]),
            assessment_reason=res["reason"],
        )
        scan.items.append(item)

    engine.db.save_scan(scan)
    print(f"Successfully seeded demo scan: {scan.scan_id} with {len(scan.items)} items!")

if __name__ == "__main__":
    seed()

