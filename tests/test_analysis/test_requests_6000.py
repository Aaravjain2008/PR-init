"""Acceptance test for tasks 2 to 6 based on psf/requests#6000."""

import json
from pathlib import Path
import unittest

from repoguard.analysis.extractor import ClaimExtractor
from repoguard.analysis.verifier import ClaimVerifier
from repoguard.models import ClaimVerdict, OverallAssessment, ItemResult, ItemType
from repoguard.engine import RepoGuardEngine


class MockGitHubClient:
    def __init__(self, mock_ctx):
        self._mock_ctx = mock_ctx

    def get_repo_tree(self, owner, repo, ref="HEAD", recursive=True):
        return self._mock_ctx["tree"]

    def get_file_content(self, owner, repo, path, ref="HEAD"):
        return self._mock_ctx["files"].get(path)

    def get_pr_files(self, owner, repo, number):
        return []

    def get_tags(self, owner, repo):
        return []

    def get_releases(self, owner, repo):
        return []


class TestRequests6000Acceptance(unittest.TestCase):
    """Test verification against psf/requests#6000 fixture."""

    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent.parent / "fixtures" / "requests_6000.json"
        with open(fixture_path) as f:
            cls.data = json.load(f)

        cls.extractor = ClaimExtractor()
        cls.claims = cls.extractor.extract_claims(cls.data["body"])
        client = MockGitHubClient(cls.data["mock_repo_context"])
        cls.verifier = ClaimVerifier(client, "psf", "requests")
        cls.verified_claims = cls.verifier.verify_claims(
            cls.claims, item_data={"item_type": "issue", "number": 6000}
        )

    def test_traceback_is_primary_claim(self):
        """Traceback exception is extracted as the first/primary claim."""
        self.assertGreater(len(self.verified_claims), 0)
        first_claim = self.verified_claims[0]
        self.assertIn("ValueError", first_claim.text)
        self.assertIn("too many values to unpack", first_claim.text)

    def test_no_stdlib_or_third_party_contradicted(self):
        """No standard library or third-party symbol/module is marked contradicted."""
        contradicted = [c for c in self.verified_claims if c.verdict == ClaimVerdict.CONTRADICTED]
        contradicted_texts = [c.text for c in contradicted]
        for name in [
            "os.getenv", "start_timestamp", "end_timestamp", "load_dotenv",
            "StackOverflow", "datetime", "timezone", "API_KEY"
        ]:
            for ct in contradicted_texts:
                self.assertNotIn(name, ct)

    def test_no_nonexistent_file_supported(self):
        """No non-existent file like test.py or venv paths is marked supported."""
        supported = [c for c in self.verified_claims if c.verdict == ClaimVerdict.SUPPORTED]
        supported_texts = [c.text for c in supported]
        for name in ["test.py", "weather_provider", "site-packages"]:
            for st in supported_texts:
                self.assertNotIn(name, st)

    def test_overall_assessment_is_insufficient_evidence(self):
        """Overall assessment is honest (Insufficient evidence) with clear reason."""
        engine = RepoGuardEngine.__new__(RepoGuardEngine)
        item_res = ItemResult(
            item_type=ItemType.ISSUE,
            number=6000,
            title=self.data["title"],
            author="weather_dev",
            url="http://example.com",
            claims=self.verified_claims,
        )
        engine._assess_item(item_res)
        self.assertEqual(item_res.assessment, OverallAssessment.INSUFFICIENT_EVIDENCE)
        self.assertIn("could not be verified", item_res.assessment_reason.lower())

