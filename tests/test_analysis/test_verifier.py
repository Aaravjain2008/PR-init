"""Tests for the ClaimVerifier module."""

import unittest
from unittest.mock import MagicMock

from repoguard.models import (
    Claim,
    ClaimVerdict,
    Confidence,
    EvidenceType,
    FindingCategory,
)
from repoguard.analysis.verifier import ClaimVerifier


class DummyGitHubClient:
    """Mock GitHub client providing mock repository contents."""

    def __init__(self, tree=None, files=None, pr_files=None):
        self._tree = tree or []
        self._files = files or {}
        self._pr_files = pr_files or []

    def get_repo_tree(self, owner, repo, ref="HEAD", recursive=True):
        return self._tree

    def get_file_content(self, owner, repo, path, ref="HEAD"):
        return self._files.get(path)

    def get_pr_files(self, owner, repo, number):
        return self._pr_files


class TestClaimVerifier(unittest.TestCase):
    """Test claim verification against simulated repo structures."""

    def setUp(self):
        self.tree = [
            {"path": "src/parser.py", "type": "blob"},
            {"path": "src/utils.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},
            {"path": "pyproject.toml", "type": "blob"},
        ]
        self.files = {
            "src/parser.py": "def parse_tokens(stream):\n    if not stream:\n        return []\n    return stream.split()\n",
            "src/utils.py": "class TokenStream:\n    def __init__(self):\n        pass\n",
            "README.md": "# Parser Project\nThis parser handles empty input gracefully by returning an empty list.\n",
            "pyproject.toml": '[project]\nname = "myparser"\nversion = "1.4.0"\n',
        }
        self.client = DummyGitHubClient(tree=self.tree, files=self.files)
        self.verifier = ClaimVerifier(self.client, "testowner", "testrepo")

    def test_file_exists_verification(self):
        claim = Claim(text="The bug is in src/parser.py line 2")
        results = self.verifier.verify_claims([claim])
        self.assertEqual(len(results), 1)
        c = results[0]
        # Should find file exists
        file_ev = [e for e in c.evidence if e.evidence_type == EvidenceType.FILE_EXISTS]
        self.assertTrue(len(file_ev) > 0)
        self.assertEqual(c.verdict, ClaimVerdict.SUPPORTED)

    def test_file_not_found_verification(self):
        claim = Claim(text="Problem exists in nonexistent/missing_file.py")
        results = self.verifier.verify_claims([claim])
        c = results[0]
        missing_ev = [e for e in c.evidence if e.evidence_type == EvidenceType.FILE_NOT_FOUND]
        self.assertTrue(len(missing_ev) > 0)
        self.assertEqual(c.verdict, ClaimVerdict.CONTRADICTED)

    def test_symbol_search(self):
        claim = Claim(text="TokenStream fails to initialize")
        results = self.verifier.verify_claims([claim])
        c = results[0]
        sym_ev = [e for e in c.evidence if e.evidence_type == EvidenceType.SYMBOL_FOUND]
        self.assertTrue(len(sym_ev) > 0)
        self.assertEqual(c.verdict, ClaimVerdict.SUPPORTED)

    def test_version_verification(self):
        claim = Claim(text="Tested with version 1.4.0")
        results = self.verifier.verify_claims([claim])
        c = results[0]
        ver_ev = [e for e in c.evidence if e.evidence_type == EvidenceType.VERSION_CHECK]
        self.assertTrue(len(ver_ev) > 0)

    def test_pr_diff_verification(self):
        pr_files = [
            {"filename": "src/parser.py", "patch": "@@ -1,3 +1,4 @@\n+    # new comment", "status": "modified"}
        ]
        client = DummyGitHubClient(tree=self.tree, files=self.files, pr_files=pr_files)
        verifier = ClaimVerifier(client, "testowner", "testrepo")

        claim = Claim(text="Modifies src/parser.py to fix edge case")
        results = verifier.verify_claims([claim], item_data={"item_type": "pull_request", "number": 10, "diff_files": pr_files})
        c = results[0]
        diff_ev = [e for e in c.evidence if e.evidence_type == EvidenceType.DIFF_MATCH]
        self.assertTrue(len(diff_ev) > 0)

    def test_empty_claims_list(self):
        results = self.verifier.verify_claims([])
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()

