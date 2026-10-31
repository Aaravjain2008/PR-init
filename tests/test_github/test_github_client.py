"""Tests for the GitHub API client."""

import unittest
from unittest.mock import MagicMock, patch
import requests

from repoguard.github.client import (
    GitHubClient,
    GitHubAPIError,
    parse_github_url,
)


class TestParseGitHubUrl(unittest.TestCase):
    """Test URL parsing for different GitHub URL formats."""

    def test_owner_repo_shorthand(self):
        owner, repo, itype, num = parse_github_url("pallets/flask")
        self.assertEqual(owner, "pallets")
        self.assertEqual(repo, "flask")
        self.assertIsNone(itype)
        self.assertIsNone(num)

    def test_full_https_repo_url(self):
        owner, repo, itype, num = parse_github_url("https://github.com/torvalds/linux")
        self.assertEqual(owner, "torvalds")
        self.assertEqual(repo, "linux")
        self.assertIsNone(itype)
        self.assertIsNone(num)

    def test_repo_url_with_git_suffix(self):
        owner, repo, itype, num = parse_github_url("https://github.com/psf/requests.git")
        self.assertEqual(owner, "psf")
        self.assertEqual(repo, "requests")

    def test_issue_url(self):
        owner, repo, itype, num = parse_github_url("https://github.com/curl/curl/issues/12345")
        self.assertEqual(owner, "curl")
        self.assertEqual(repo, "curl")
        self.assertEqual(itype, "issue")
        self.assertEqual(num, 12345)

    def test_pr_url(self):
        owner, repo, itype, num = parse_github_url("https://github.com/rust-lang/rust/pull/999")
        self.assertEqual(owner, "rust-lang")
        self.assertEqual(repo, "rust")
        self.assertEqual(itype, "pull_request")
        self.assertEqual(num, 999)

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            parse_github_url("not a github url")


class TestGitHubClient(unittest.TestCase):
    """Test GitHubClient with mocked HTTP requests."""

    def setUp(self):
        self.client = GitHubClient(token="mock_token_123")

    def test_headers_and_auth(self):
        self.assertIn("Authorization", self.client._session.headers)
        self.assertEqual(self.client._session.headers["Authorization"], "Bearer mock_token_123")
        self.assertEqual(self.client._session.headers["Accept"], "application/vnd.github+json")

    @patch.object(requests.Session, "request")
    def test_get_issue_success(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 1, "number": 42, "title": "Crash on empty input"}
        mock_req.return_value = mock_resp

        issue = self.client.get_issue("owner", "repo", 42)
        self.assertEqual(issue["number"], 42)
        self.assertEqual(issue["title"], "Crash on empty input")
        mock_req.assert_called_once()

    @patch.object(requests.Session, "request")
    def test_rate_limit_exceeded(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}
        mock_resp.text = "API rate limit exceeded"
        mock_req.return_value = mock_resp

        with self.assertRaises(GitHubAPIError) as ctx:
            self.client.get_issue("owner", "repo", 1)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("rate limit exceeded", ctx.exception.message)

    @patch.object(requests.Session, "request")
    def test_not_found_error(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_req.return_value = mock_resp

        with self.assertRaises(GitHubAPIError) as ctx:
            self.client.get_issue("owner", "repo", 99999)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch.object(requests.Session, "request")
    def test_network_error(self, mock_req):
        mock_req.side_effect = requests.ConnectionError("Connection refused")
        with self.assertRaises(GitHubAPIError) as ctx:
            self.client.get_issue("owner", "repo", 1)
        self.assertIsNone(ctx.exception.status_code)

    @patch.object(requests.Session, "request")
    def test_get_repo_tree(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tree": [
                {"path": "src/main.py", "type": "blob"},
                {"path": "README.md", "type": "blob"}
            ]
        }
        mock_req.return_value = mock_resp

        tree = self.client.get_repo_tree("owner", "repo")
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0]["path"], "src/main.py")

    @patch.object(requests.Session, "request")
    def test_get_file_content_base64(self, mock_req):
        import base64
        content = "print('hello world')\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": encoded, "encoding": "base64"}
        mock_req.return_value = mock_resp

        data = self.client.get_file_content("owner", "repo", "main.py")
        self.assertEqual(data, content)


if __name__ == "__main__":
    unittest.main()
