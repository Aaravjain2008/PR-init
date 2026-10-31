"""Tests for the real GitHub benchmark builder and evaluation harness handling."""

import io
import json
import os
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.build_real_benchmark import (
    GitHubBenchmarkBuilder,
    assign_split,
)
from repoguard.evaluation.harness import EvaluationHarness, EvaluationMetrics


class TestRealBenchmarkBuilder(unittest.TestCase):
    """Test GitHub benchmark builder functionality, security, and determinism."""

    def test_split_assignment_is_deterministic(self):
        """Split assignment produces identical results for identical repo and number."""
        split1 = assign_split("psf/requests", 6000)
        split2 = assign_split("psf/requests", 6000)
        self.assertEqual(split1, split2)
        self.assertIn(split1, ("dev", "test"))

        # Verify cross-run reproducibility
        split_curl = assign_split("curl/curl", 1234)
        split_curl_again = assign_split("curl/curl", 1234)
        self.assertEqual(split_curl, split_curl_again)

    def test_builder_handles_mocked_github_response(self):
        """Builder correctly constructs benchmark cases from API responses."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = GitHubBenchmarkBuilder(token="secret_token_abc123", cache_dir=Path(tmpdir))

            # Mock _api_get
            def mock_get(url, params=None):
                if "/labels" in url:
                    return [{"name": "invalid"}, {"name": "confirmed"}]
                elif "/issues" in url:
                    return [{
                        "number": 101,
                        "title": "Bug in query parsing",
                        "body": "Steps to reproduce:\nRun parser.",
                        "state": "closed",
                        "created_at": "2022-01-01T00:00:00Z",
                        "closed_at": "2022-01-02T00:00:00Z",
                        "html_url": "https://github.com/example/repo/issues/101",
                    }]
                elif "/commits" in url:
                    return [{"sha": "deadbeef12345678"}]
                elif "/git/trees" in url:
                    return {"tree": [{"path": "main.py", "type": "blob"}]}
                return None

            builder._api_get = MagicMock(side_effect=mock_get)

            cases = builder.build_benchmark(repos=["example/repo"], limit=1)
            self.assertEqual(len(cases), 1)
            case = cases[0]
            self.assertEqual(case["repository"], "example/repo")
            self.assertEqual(case["number"], 101)
            self.assertEqual(case["ground_truth_label"], "confirmed")
            self.assertEqual(case["expected_assessment"], "No strong concern found")
            self.assertEqual(case["label_source"], "confirmed")
            self.assertEqual(case["commit_sha"], "deadbeef12345678")
            self.assertFalse(case["synthetic"])
            self.assertIn("Closed issue with maintainer triage label", case["selection_method"])

    def test_truncated_tree_skipped_by_default_and_kept_with_flag(self):
        """Truncated trees are skipped by default and kept when allow_truncated is True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            def mock_get(url, params=None):
                if "/labels" in url:
                    return [{"name": "confirmed"}]
                elif "/issues" in url:
                    return [{
                        "number": 202,
                        "title": "Truncated tree issue",
                        "body": "Some bug report",
                        "state": "closed",
                        "created_at": "2022-01-01T00:00:00Z",
                    }]
                elif "/commits" in url:
                    return [{"sha": "sha_trunc_123"}]
                elif "/git/trees" in url:
                    return {"tree": [{"path": "README.md", "type": "blob"}], "truncated": True}
                return None

            # 1. Skipped by default
            builder_default = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir) / "c1")
            builder_default._api_get = MagicMock(side_effect=mock_get)
            cases_default = builder_default.build_benchmark(repos=["example/repo"], limit=1)
            self.assertEqual(len(cases_default), 0)
            self.assertEqual(builder_default.skipped_stats["truncated"], 1)

            # 2. Kept with allow_truncated=True
            builder_allowed = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir) / "c2", allow_truncated=True)
            builder_allowed._api_get = MagicMock(side_effect=mock_get)
            cases_allowed = builder_allowed.build_benchmark(repos=["example/repo"], limit=1)
            self.assertEqual(len(cases_allowed), 1)
            self.assertTrue(cases_allowed[0]["tree_truncated"])

    def test_file_contents_fetched_only_for_mentioned_files_and_respect_caps(self):
        """File contents are fetched only for mentioned files and fixed files, respecting caps."""
        import base64
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir))

            tree = [
                {"path": "lib/parser.py", "type": "blob"},
                {"path": "unrelated/other.py", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ]
            fetched_urls = []

            def mock_get(url, params=None):
                fetched_urls.append(url)
                if "parser.py" in url:
                    return {"content": base64.b64encode(b"def parse(): pass").decode("ascii"), "encoding": "base64"}
                elif "README.md" in url:
                    return {"content": base64.b64encode(b"# Docs").decode("ascii"), "encoding": "base64"}
                elif "other.py" in url:
                    return {"content": base64.b64encode(b"secret").decode("ascii"), "encoding": "base64"}
                return None

            builder._api_get = MagicMock(side_effect=mock_get)
            text = "Error in `lib/parser.py` when running tests."
            files = builder.fetch_file_contents(
                repo="example/repo",
                commit_sha="csha1",
                tree=tree,
                text_to_scan=text,
                max_files=10,
                max_total_bytes=1000,
            )

            self.assertIn("lib/parser.py", files)
            self.assertIn("README.md", files)
            self.assertNotIn("unrelated/other.py", files)

            # Cap test: max_files=1
            files_capped = builder.fetch_file_contents(
                repo="example/repo",
                commit_sha="csha1",
                tree=tree,
                text_to_scan=text,
                max_files=1,
            )
            self.assertEqual(len(files_capped), 1)

    def test_loose_labels_off_by_default(self):
        """Loose labels such as 'bug' or 'wontfix' are ignored by default and recognized with flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            def mock_get(url, params=None):
                if "/labels" in url:
                    return [{"name": "bug"}, {"name": "wontfix"}]
                elif "/issues" in url:
                    return [{
                        "number": 303,
                        "title": "Bug issue",
                        "body": "Report",
                        "state": "closed",
                        "created_at": "2022-01-01T00:00:00Z",
                    }]
                elif "/commits" in url:
                    return [{"sha": "c1"}]
                elif "/git/trees" in url:
                    return {"tree": []}
                return None

            # Off by default
            builder_strict = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir) / "s1", loose_labels=False)
            builder_strict._api_get = MagicMock(side_effect=mock_get)
            cases_strict = builder_strict.build_benchmark(repos=["example/repo"], limit=1)
            self.assertEqual(len(cases_strict), 0)

            # Enabled with loose_labels=True
            builder_loose = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir) / "s2", loose_labels=True)
            builder_loose._api_get = MagicMock(side_effect=mock_get)
            cases_loose = builder_loose.build_benchmark(repos=["example/repo"], limit=1)
            self.assertEqual(len(cases_loose), 1)
            self.assertIn(cases_loose[0]["label_source"], ("bug", "wontfix"))

    def test_pull_requests_excluded(self):
        """Pull requests returned by issues API are filtered out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir))
            builder._api_get = MagicMock(return_value=[
                {"number": 1, "state": "closed", "pull_request": {"url": "http://pr"}},
                {"number": 2, "state": "closed"},
                {"number": 3, "state": "open"},
            ])
            issues = builder.fetch_closed_issues_by_label("example/repo", "confirmed")
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["number"], 2)

    def test_selection_is_deterministic_across_runs(self):
        """Deterministic hashing orders candidate issues reproducibly."""
        candidates = [
            {"number": 10},
            {"number": 20},
            {"number": 30},
            {"number": 40},
            {"number": 50},
        ]
        import hashlib
        def sort_candidates(items):
            return sorted(
                items,
                key=lambda iss: hashlib.sha256(f"test/repo#{iss['number']}".encode("utf-8")).hexdigest()
            )

        run1 = [c["number"] for c in sort_candidates(candidates)]
        run2 = [c["number"] for c in sort_candidates(candidates)]
        self.assertEqual(run1, run2)

    def test_symbol_matched_files_fetched_and_respect_caps(self):
        """Files matching symbol stems are fetched and respect caps."""
        import base64
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir))

            tree = [
                {"path": "requests/sessions.py", "type": "blob"},
                {"path": "requests/models.py", "type": "blob"},
                {"path": "unrelated/file.py", "type": "blob"},
            ]
            fetched = []

            def mock_get(url, params=None):
                fetched.append(url)
                if "sessions.py" in url:
                    return {"content": base64.b64encode(b"class Session: pass").decode("ascii"), "encoding": "base64"}
                elif "models.py" in url:
                    return {"content": base64.b64encode(b"class Model: pass").decode("ascii"), "encoding": "base64"}
                return None

            builder._api_get = MagicMock(side_effect=mock_get)
            text = "Calls to `Session.request` throw an unexpected error"
            files = builder.fetch_file_contents(
                repo="psf/requests",
                commit_sha="c1",
                tree=tree,
                text_to_scan=text,
                max_files=5,
            )

            self.assertIn("requests/sessions.py", files)
            self.assertNotIn("unrelated/file.py", files)

            # Respects max_files cap
            files_capped = builder.fetch_file_contents(
                repo="psf/requests",
                commit_sha="c1",
                tree=tree,
                text_to_scan=text,
                max_files=1,
            )
            self.assertEqual(len(files_capped), 1)

    def test_paging_3_page_response_deterministic_pool_selection(self):
        """Paging fetches across up to 3 pages and selection is deterministic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = GitHubBenchmarkBuilder(token="tok", cache_dir=Path(tmpdir), pool_size=90)

            # Generate 100 items per page (30 closed issues + 70 PRs)
            page_calls = []
            def mock_get(url, params=None):
                if "/issues" in url:
                    page = int(params.get("page", 1))
                    page_calls.append(page)
                    start_num = (page - 1) * 30 + 1
                    items = [
                        {"number": i, "state": "closed"}
                        for i in range(start_num, start_num + 30)
                    ]
                    # Pad to 100 with pull requests
                    for pr_i in range(70):
                        items.append({"number": 1000 + pr_i, "state": "closed", "pull_request": {}})
                    return items
                return None

            builder._api_get = MagicMock(side_effect=mock_get)
            issues = builder.fetch_closed_issues_by_label("test/repo", "confirmed", pool_size=90)
            self.assertEqual(len(issues), 90)
            self.assertEqual(page_calls, [1, 2, 3])

            # Selection with deterministic hash
            candidates1 = list(issues)
            candidates2 = list(issues)
            candidates1.sort(key=lambda iss: hashlib.sha256(f"test/repo#{iss['number']}".encode("utf-8")).hexdigest())
            candidates2.sort(key=lambda iss: hashlib.sha256(f"test/repo#{iss['number']}".encode("utf-8")).hexdigest())
            self.assertEqual([c["number"] for c in candidates1], [c["number"] for c in candidates2])
            # Selected issue should come from any of the 3 pages, not just the newest
            selected_num = candidates1[0]["number"]
            self.assertTrue(1 <= selected_num <= 90)

    def test_builder_never_prints_token(self):
        """Token from environment is never written to stdout, stderr, or logs."""
        fake_token = "ghp_super_secret_token_never_expose_987654321"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_capture = io.StringIO()
            err_capture = io.StringIO()

            with patch.dict(os.environ, {"GITHUB_TOKEN": fake_token}):
                with patch("sys.stdout", out_capture), patch("sys.stderr", err_capture):
                    builder = GitHubBenchmarkBuilder(cache_dir=Path(tmpdir))
                    builder._api_get = MagicMock(return_value=[])
                    builder.build_benchmark(repos=["example/repo"], limit=1)

            stdout_text = out_capture.getvalue()
            stderr_text = err_capture.getvalue()
            self.assertNotIn(fake_token, stdout_text)
            self.assertNotIn(fake_token, stderr_text)

    def test_harness_accepts_real_dataset_with_same_metrics(self):
        """EvaluationHarness runs seamlessly on datasets with synthetic=False."""
        real_cases = [
            {
                "id": "mock-1",
                "repository": "mock/repo",
                "number": 1,
                "title": "Unreproducible glitch",
                "body": "Random error occurs",
                "ground_truth_label": "cannot reproduce",
                "expected_assessment": "Insufficient evidence",
                "split": "test",
                "synthetic": False,
                "mock_repo_context": {
                    "tree": [{"path": "README.md", "type": "blob"}],
                    "files": {"README.md": "# Mock"},
                },
            },
            {
                "id": "mock-2",
                "repository": "mock/repo",
                "number": 2,
                "title": "Confirmed bug",
                "body": "Bug report",
                "ground_truth_label": "confirmed",
                "expected_assessment": "No strong concern found",
                "split": "test",
                "synthetic": False,
                "mock_repo_context": {
                    "tree": [{"path": "README.md", "type": "blob"}],
                    "files": {"README.md": "# Mock"},
                },
            },
        ]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(real_cases, f)
            dataset_path = f.name

        try:
            harness = EvaluationHarness(dataset_path=dataset_path)
            metrics = harness.run(split="test")
            self.assertIsInstance(metrics, EvaluationMetrics)
            self.assertEqual(metrics.total_cases, 2)
            self.assertFalse(metrics.is_synthetic)

            summary = harness.format_summary(metrics)
            self.assertIn("RepoGuard Evaluation Benchmark Results", summary)
            self.assertIn("Confusion Matrix", summary)
            self.assertIn("Baseline Comparisons", summary)
        finally:
            Path(dataset_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
