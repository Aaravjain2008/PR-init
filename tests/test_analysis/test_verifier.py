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

    def test_symbol_present_gives_supported(self):
        claim = Claim(text="TokenStream class is broken")
        results = self.verifier.verify_claims([claim])
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_repo_symbol_absent_all_candidates_read_gives_contradicted(self):
        claim = Claim(text="testrepo.NonExistentClass crashes on start")
        results = self.verifier.verify_claims([claim])
        self.assertEqual(results[0].verdict, ClaimVerdict.CONTRADICTED)
        sym_ev = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertTrue(len(sym_ev) > 0)

    def test_repo_symbol_absent_one_unreadable_candidate_gives_unverifiable(self):
        # Remove src/utils.py content so it is unavailable (None)
        files = dict(self.files)
        files.pop("src/utils.py")
        client = DummyGitHubClient(tree=self.tree, files=files)
        verifier = ClaimVerifier(client, "testowner", "testrepo")

        claim = Claim(text="testrepo.NonExistentClass crashes on start")
        results = verifier.verify_claims([claim])
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found_ev = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found_ev), 0)

    def test_stdlib_and_third_party_names_stay_unverifiable(self):
        claim = Claim(text="requests.Session throws error")
        results = self.verifier.verify_claims([claim])
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found_ev = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found_ev), 0)

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

    # --- Prompt G: package-qualified symbol tests ---

    def test_pkg_qualified_symbol_exists_gives_supported(self):
        """requests.Session in a requests repo should be SUPPORTED."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "requests/api.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "",
            "requests/sessions.py": "class Session:\n    def send(self): pass\n",
            "requests/api.py": "def get(): pass\n",
            "setup.py": "",
        }
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        from repoguard.analysis.extractor import ClaimExtractor
        claims = ClaimExtractor().extract_claims("`requests.Session`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_pkg_qualified_symbol_missing_all_readable_gives_contradicted(self):
        """requests.Foo in a requests repo (all files readable) should be CONTRADICTED."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "requests/api.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "",
            "requests/sessions.py": "class Session:\n    def send(self): pass\n",
            "requests/api.py": "def get(): pass\n",
            "setup.py": "",
        }
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        from repoguard.analysis.extractor import ClaimExtractor
        claims = ClaimExtractor().extract_claims("`requests.Foo`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.CONTRADICTED)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertTrue(len(not_found) > 0)

    def test_pkg_qualified_symbol_with_unreadable_file_gives_unverifiable(self):
        """requests.Foo with an unreadable candidate file should be UNVERIFIABLE."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "",
            # requests/sessions.py intentionally absent (unreadable)
            "setup.py": "",
        }
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        from repoguard.analysis.extractor import ClaimExtractor
        claims = ClaimExtractor().extract_claims("`requests.Foo`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found), 0)

    def test_pkg_qualified_symbol_with_parens_gives_supported(self):
        """`requests.Session()` with trailing parens should still find Session."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "",
            "requests/sessions.py": "class Session:\n    def send(self): pass\n",
        }
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        from repoguard.analysis.extractor import ClaimExtractor
        claims = ClaimExtractor().extract_claims("`requests.Session()`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_stdlib_symbol_stays_unverifiable(self):
        """os.getenv should stay UNVERIFIABLE (stdlib, not a repo symbol)."""
        claim = Claim(text="References symbol: os.getenv")
        results = self.verifier.verify_claims([claim])
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found), 0)

    # --- Prompt H: dotted-symbol member verification tests ---

    def _requests_verifier(self, extra_files=None):
        """Return a ClaimVerifier configured for psf/requests with a small mock repo."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "requests/api.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "from .api import get\n",
            "requests/sessions.py": "class Session:\n    def send(self): pass\n",
            "requests/api.py": "def get(): pass\n",
            "setup.py": "",
        }
        if extra_files:
            files.update(extra_files)
        return ClaimVerifier(DummyGitHubClient(tree=tree, files=files), "psf", "requests")

    def test_dotted_fake_method_on_real_class_is_contradicted(self):
        """`Session.nonexistent_method()` — method does not exist → CONTRADICTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = self._requests_verifier()
        claims = ClaimExtractor().extract_claims("`Session.nonexistent_method()`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.CONTRADICTED)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertTrue(len(not_found) > 0)

    def test_dotted_fake_member_on_real_module_is_contradicted(self):
        """`api.nope` — member does not exist in api module → CONTRADICTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = self._requests_verifier()
        claims = ClaimExtractor().extract_claims("`api.nope`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.CONTRADICTED)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertTrue(len(not_found) > 0)

    def test_session_send_stays_supported(self):
        """`Session.send()` — method exists → SUPPORTED (regression guard)."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = self._requests_verifier()
        claims = ClaimExtractor().extract_claims("`Session.send()`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_pkg_qualified_session_still_supported(self):
        """`requests.Session` — pkg-qualified existing symbol → SUPPORTED (regression guard)."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = self._requests_verifier()
        claims = ClaimExtractor().extract_claims("`requests.Session`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_dotted_fake_method_unreadable_candidate_gives_unverifiable(self):
        """`Session.nonexistent_method()` with unreadable sessions.py → UNVERIFIABLE."""
        from repoguard.analysis.extractor import ClaimExtractor
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        # sessions.py intentionally absent (unreadable)
        files = {
            "requests/__init__.py": "from .api import get\n",
            "setup.py": "",
        }
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        claims = ClaimExtractor().extract_claims("`Session.nonexistent_method()`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found), 0)

    def test_os_getenv_stays_unverifiable_prompt_h(self):
        """`os.getenv` — stdlib dotted symbol → UNVERIFIABLE."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = self._requests_verifier()
        claims = ClaimExtractor().extract_claims("`os.getenv`")
        results = verifier.verify_claims(claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)
        not_found = [e for e in results[0].evidence if e.evidence_type == EvidenceType.SYMBOL_NOT_FOUND]
        self.assertEqual(len(not_found), 0)

    # --- Prompt I: version and line-number verification tests ---

    def _prompt_i_client(self, tags=None, raises_tags=False):
        """DummyGitHubClient for the Prompt I mock repo."""
        tree = [
            {"path": "requests/__init__.py", "type": "blob"},
            {"path": "requests/sessions.py", "type": "blob"},
            {"path": "src/utils/helpers.py", "type": "blob"},
            {"path": "setup.py", "type": "blob"},
        ]
        files = {
            "requests/__init__.py": "__version__='2.28.1'",
            "requests/sessions.py": "import os\nclass Session:\n    def send(self):\n        return 1\n",
            "src/utils/helpers.py": "",
            "setup.py": "setup(name='requests')",
        }

        class _Client(DummyGitHubClient):
            def __init__(self, _tags, _raises):
                super().__init__(tree=tree, files=files)
                self.__tags = _tags
                self.__raises = _raises
            def get_tags(self, o, r):
                if self.__raises:
                    raise Exception("no tags")
                return self.__tags if self.__tags is not None else []
            def get_releases(self, o, r):
                if self.__raises:
                    raise Exception("no releases")
                return []

        return _Client(tags, raises_tags)

    def test_version_in_init_py_gives_supported(self):
        """Version in __init__.__version__ should be SUPPORTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(self._prompt_i_client(), "psf", "requests")
        claims = ClaimExtractor().extract_claims("Version 2.28.1 is affected")
        results = verifier.verify_claims(claims)
        ver_claims = [c for c in results if "2.28.1" in c.text]
        self.assertTrue(len(ver_claims) > 0)
        self.assertEqual(ver_claims[0].verdict, ClaimVerdict.SUPPORTED)

    def test_unknown_version_no_tags_gives_unverifiable(self):
        """Version 9.9.9, tags unreadable → UNVERIFIABLE (may be older release)."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(self._prompt_i_client(raises_tags=True), "psf", "requests")
        claims = ClaimExtractor().extract_claims("version 9.9.9")
        results = verifier.verify_claims(claims)
        ver_claims = [c for c in results if "9.9.9" in c.text]
        self.assertTrue(len(ver_claims) > 0)
        self.assertEqual(ver_claims[0].verdict, ClaimVerdict.UNVERIFIABLE)

    def test_unknown_version_tags_read_gives_contradicted(self):
        """Version 9.9.9, tags were read, not among them → CONTRADICTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(
            self._prompt_i_client(tags=[{"name": "v2.28.1"}]), "psf", "requests"
        )
        claims = ClaimExtractor().extract_claims("version 9.9.9")
        results = verifier.verify_claims(claims)
        ver_claims = [c for c in results if "9.9.9" in c.text]
        self.assertTrue(len(ver_claims) > 0)
        self.assertEqual(ver_claims[0].verdict, ClaimVerdict.CONTRADICTED)

    def test_version_matches_tag_gives_supported(self):
        """Version that matches a repo tag should be SUPPORTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(
            self._prompt_i_client(tags=[{"name": "v9.9.9"}]), "psf", "requests"
        )
        claims = ClaimExtractor().extract_claims("version 9.9.9")
        results = verifier.verify_claims(claims)
        ver_claims = [c for c in results if "9.9.9" in c.text]
        self.assertTrue(len(ver_claims) > 0)
        self.assertEqual(ver_claims[0].verdict, ClaimVerdict.SUPPORTED)

    def test_no_version_source_gives_unverifiable(self):
        """If no version file exists at all, version claim is UNVERIFIABLE."""
        from repoguard.analysis.extractor import ClaimExtractor
        tree = [{"path": "src/main.py", "type": "blob"}]
        files = {"src/main.py": "def main(): pass\n"}
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "owner", "repo")
        claims = ClaimExtractor().extract_claims("version 1.0.0")
        results = verifier.verify_claims(claims)
        ver_claims = [c for c in results if "1.0.0" in c.text]
        self.assertTrue(len(ver_claims) > 0)
        self.assertEqual(ver_claims[0].verdict, ClaimVerdict.UNVERIFIABLE)

    def test_line_in_range_with_token_gives_supported(self):
        """`sessions.py` line 3 (def send) — line 3 is 'def send' → SUPPORTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(self._prompt_i_client(), "psf", "requests")
        claims = ClaimExtractor().extract_claims(
            "`requests/sessions.py` line 3 (def send)"
        )
        line_claims = [c for c in claims if "line" in c.text.lower() and "3" in c.text]
        self.assertTrue(len(line_claims) > 0, "No line-3 claim extracted")
        results = verifier.verify_claims(line_claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.SUPPORTED)

    def test_line_out_of_range_gives_contradicted(self):
        """line 400 of sessions.py (only 4 lines) → CONTRADICTED."""
        from repoguard.analysis.extractor import ClaimExtractor
        verifier = ClaimVerifier(self._prompt_i_client(), "psf", "requests")
        claims = ClaimExtractor().extract_claims(
            "line 400 of `requests/sessions.py`"
        )
        line_claims = [c for c in claims if "line" in c.text.lower() and "400" in c.text]
        self.assertTrue(len(line_claims) > 0, "No line-400 claim extracted")
        results = verifier.verify_claims(line_claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.CONTRADICTED)
        mismatch = [e for e in results[0].evidence if e.evidence_type == EvidenceType.LINE_MISMATCH]
        self.assertTrue(len(mismatch) > 0)

    def test_line_claim_unreadable_file_gives_unverifiable(self):
        """Line claim where the file cannot be read → UNVERIFIABLE."""
        from repoguard.analysis.extractor import ClaimExtractor
        tree = [{"path": "requests/sessions.py", "type": "blob"}]
        files = {}  # file in tree but content unavailable
        client = DummyGitHubClient(tree=tree, files=files)
        verifier = ClaimVerifier(client, "psf", "requests")
        claims = ClaimExtractor().extract_claims(
            "`requests/sessions.py` line 3"
        )
        line_claims = [c for c in claims if "line" in c.text.lower() and "3" in c.text]
        self.assertTrue(len(line_claims) > 0, "No line-3 claim extracted")
        results = verifier.verify_claims(line_claims)
        self.assertEqual(results[0].verdict, ClaimVerdict.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()

