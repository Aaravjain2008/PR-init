"""Tests for central RepoGuardEngine."""

import unittest
from unittest.mock import MagicMock, patch

from repoguard.engine import RepoGuardEngine
from repoguard.models import (
    EngineConfig,
    ItemResult,
    ItemType,
    OverallAssessment,
    Claim,
    ClaimVerdict,
    Confidence,
)


class TestRepoGuardEngine(unittest.TestCase):
    """Test engine orchestration, scanning, and roll-up assessment."""

    def setUp(self):
        self.config = EngineConfig(db_path=":memory:")
        self.engine = RepoGuardEngine(self.config)

    def test_engine_initialization(self):
        status = self.engine.get_status()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["version"], "1.0.0")

    def test_assessment_review_recommended_when_claims_contradicted(self):
        item = ItemResult(
            item_type=ItemType.ISSUE,
            number=1,
            title="Bogus Claim",
            author="author",
            url="http://github.com/a/b/issues/1",
            claims=[
                Claim(text="File exists", verdict=ClaimVerdict.CONTRADICTED),
                Claim(text="Function works", verdict=ClaimVerdict.SUPPORTED),
            ],
        )
        self.engine._assess_item(item)
        self.assertEqual(item.assessment, OverallAssessment.REVIEW_RECOMMENDED)

    def test_assessment_no_strong_concern_when_majority_supported(self):
        item = ItemResult(
            item_type=ItemType.ISSUE,
            number=2,
            title="Real Bug",
            author="author",
            url="http://github.com/a/b/issues/2",
            claims=[
                Claim(text="File exists", verdict=ClaimVerdict.SUPPORTED),
                Claim(text="Class exists", verdict=ClaimVerdict.SUPPORTED),
            ],
        )
        self.engine._assess_item(item)
        self.assertEqual(item.assessment, OverallAssessment.NO_STRONG_CONCERN)

    def test_assessment_insufficient_evidence_when_unverifiable(self):
        item = ItemResult(
            item_type=ItemType.ISSUE,
            number=3,
            title="Vague Issue",
            author="author",
            url="http://github.com/a/b/issues/3",
            claims=[
                Claim(text="Something broke", verdict=ClaimVerdict.UNVERIFIABLE),
            ],
        )
        self.engine._assess_item(item)
        self.assertEqual(item.assessment, OverallAssessment.INSUFFICIENT_EVIDENCE)

    @patch.object(RepoGuardEngine, "_save_scan")
    def test_scan_item_mocked(self, mock_save):
        self.engine.github.get_issue = MagicMock(return_value={
            "title": "Bug in parser.py",
            "user": {"login": "tester"},
            "html_url": "http://github.com/owner/repo/issues/10",
            "created_at": "2026-10-31T00:00:00Z",
            "body": "Crash in parser.py at line 10",
        })
        self.engine.github.get_comments = MagicMock(return_value=[])
        self.engine.github.get_repo_tree = MagicMock(return_value=[
            {"path": "parser.py", "type": "blob"}
        ])
        self.engine.github.get_file_content = MagicMock(return_value="x = 1\n")

        scan = self.engine.scan_item("owner/repo", "issue", 10)
        self.assertEqual(scan.items_scanned, 1)
        self.assertEqual(scan.items[0].number, 10)
        self.assertTrue(len(scan.items[0].claims) > 0)

    @patch.object(RepoGuardEngine, "_save_scan")
    def test_scan_repository_empty_is_complete_with_warning(self, mock_save):
        self.engine.github.list_issues = MagicMock(return_value=[])
        self.engine.github.list_pull_requests = MagicMock(return_value=[])
        self.engine.github.get_repo_tree = MagicMock(return_value=[])

        scan = self.engine.scan_repository("owner/repo", item_type=None, state="open")
        self.assertEqual(scan.scan_status.value, "complete")
        self.assertEqual(scan.scope, "issues and pull requests (open)")
        self.assertIn("No open issues or pull requests found", scan.warnings)
        self.assertEqual(len(scan.errors), 0)

    @patch.object(RepoGuardEngine, "_save_scan")
    def test_scan_repository_item_type_scope(self, mock_save):
        self.engine.github.list_issues = MagicMock(return_value=[])
        scan = self.engine.scan_repository("owner/repo", item_type="issue", state="closed")
        self.assertEqual(scan.scope, "issues (closed)")
        self.assertEqual(scan.scan_status.value, "complete")


if __name__ == "__main__":
    unittest.main()

