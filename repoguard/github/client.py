"""GitHub REST API v3 client.

Handles authentication, pagination, rate-limit detection, and URL parsing
for the GitHub API. Never logs or stores token values.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


# ── Exceptions ──────────────────────────────────────────────────────────────

class GitHubAPIError(Exception):
    """Raised when a GitHub API call fails.

    Attributes:
        status_code: HTTP status code, or ``None`` for network errors.
        message: Human-readable description of the failure.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


# ── URL parsing ─────────────────────────────────────────────────────────────

# Matches URLs like:
#   https://github.com/owner/repo
#   https://github.com/owner/repo/issues/42
#   https://github.com/owner/repo/pull/17
#   github.com/owner/repo/issues/42
_GITHUB_URL_RE = re.compile(
    r"(?:https?://)?github\.com/"
    r"(?P<owner>[A-Za-z0-9\-_.]+)/"
    r"(?P<repo>[A-Za-z0-9\-_.]+)"
    r"(?:/(?P<type>issues|pull)/(?P<number>\d+))?"
    r"(?:/.*)?$"
)

# Simple "owner/repo" shorthand.
_OWNER_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9\-_.]+)/(?P<repo>[A-Za-z0-9\-_.]+)$"
)


def parse_github_url(url: str) -> Tuple[str, str, Optional[str], Optional[int]]:
    """Extract ``(owner, repo, item_type, number)`` from a GitHub URL.

    *item_type* is ``"issue"`` or ``"pull_request"``; both it and *number*
    are ``None`` when the URL points at a repository root.

    Accepts full URLs, ``github.com/…`` without scheme, and plain
    ``owner/repo`` strings.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    url = url.strip()

    # Try owner/repo shorthand first.
    m = _OWNER_REPO_RE.match(url)
    if m:
        return m.group("owner"), m.group("repo"), None, None

    m = _GITHUB_URL_RE.match(url)
    if not m:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")

    owner = m.group("owner")
    repo = m.group("repo")
    # Strip trailing `.git`
    if repo.endswith(".git"):
        repo = repo[:-4]

    item_type_raw = m.group("type")
    number_raw = m.group("number")

    item_type: Optional[str] = None
    number: Optional[int] = None

    if item_type_raw and number_raw:
        item_type = "pull_request" if item_type_raw == "pull" else "issue"
        number = int(number_raw)

    return owner, repo, item_type, number


# ── Client ──────────────────────────────────────────────────────────────────

_BASE_URL = "https://api.github.com"


class GitHubClient:
    """Thin wrapper around the GitHub REST API v3.

    Parameters:
        token: Personal-access or fine-grained token. ``None`` for
            unauthenticated access (lower rate limits).
        timeout: Default HTTP timeout in seconds.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    # ── internal helpers ────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Issue an API request and handle errors uniformly.

        Raises:
            GitHubAPIError: On HTTP errors, rate-limit exhaustion, or
                network failures.
        """
        url = f"{_BASE_URL}{path}"
        try:
            resp = self._session.request(
                method, url, params=params, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise GitHubAPIError(
                f"Network error: {exc}", status_code=None,
            ) from exc

        # Rate-limit exhaustion: GitHub returns 403 with remaining == 0.
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "")
            if remaining == "0":
                reset_at = resp.headers.get("X-RateLimit-Reset", "")
                raise GitHubAPIError(
                    f"GitHub rate limit exceeded. "
                    f"Resets at timestamp {reset_at}.",
                    status_code=403,
                )

        if resp.status_code == 404:
            raise GitHubAPIError(
                f"Not found: {path}", status_code=404,
            )

        if resp.status_code >= 400:
            body = resp.text[:300]
            raise GitHubAPIError(
                f"GitHub API error {resp.status_code}: {body}",
                status_code=resp.status_code,
            )

        return resp

    def _get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """GET *path* and return the parsed JSON body."""
        return self._request("GET", path, params=params).json()

    def _paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 30,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """Follow ``Link: <…>; rel="next"`` headers for pagination."""
        params = dict(params or {})
        params["per_page"] = per_page

        results: List[Dict[str, Any]] = []
        url = f"{_BASE_URL}{path}"

        for _ in range(max_pages):
            try:
                resp = self._session.get(
                    url, params=params, timeout=self._timeout,
                )
            except requests.RequestException as exc:
                raise GitHubAPIError(
                    f"Network error: {exc}", status_code=None,
                ) from exc

            if resp.status_code == 403:
                remaining = resp.headers.get("X-RateLimit-Remaining", "")
                if remaining == "0":
                    reset_at = resp.headers.get("X-RateLimit-Reset", "")
                    raise GitHubAPIError(
                        f"GitHub rate limit exceeded. "
                        f"Resets at timestamp {reset_at}.",
                        status_code=403,
                    )

            if resp.status_code >= 400:
                body = resp.text[:300]
                raise GitHubAPIError(
                    f"GitHub API error {resp.status_code}: {body}",
                    status_code=resp.status_code,
                )

            page_data = resp.json()
            if not isinstance(page_data, list):
                page_data = [page_data]
            results.extend(page_data)

            # Follow next link or stop.
            link_header = resp.headers.get("Link", "")
            next_url = self._parse_next_link(link_header)
            if not next_url:
                break
            url = next_url
            params = {}  # Params are already in the next URL.

        return results

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        """Extract the URL for ``rel="next"`` from a Link header."""
        for part in link_header.split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    return match.group(1)
        return None

    # ── public API ──────────────────────────────────────────────────────

    def get_issue(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        """Fetch a single issue by number.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._get(f"/repos/{owner}/{repo}/issues/{number}")

    def get_pull_request(
        self, owner: str, repo: str, number: int,
    ) -> Dict[str, Any]:
        """Fetch a single pull request by number.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}")

    def get_comments(
        self, owner: str, repo: str, number: int,
    ) -> List[Dict[str, Any]]:
        """Fetch all comments on an issue or PR, with pagination.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            per_page=100,
            max_pages=10,
        )

    def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 30,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """List issues with pagination and state filter.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state},
            per_page=per_page,
            max_pages=max_pages,
        )

    def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 30,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """List pull requests with pagination.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state},
            per_page=per_page,
            max_pages=max_pages,
        )

    def get_pr_files(
        self, owner: str, repo: str, number: int,
    ) -> List[Dict[str, Any]]:
        """Get the list of files changed in a pull request.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            per_page=100,
            max_pages=10,
        )

    def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: Optional[str] = None,
    ) -> Optional[str]:
        """Get the decoded text content of a file.

        Returns:
            The file content as a string, or ``None`` if the path is not a
            file (e.g. it is a directory or symlink).

        Raises:
            GitHubAPIError: On any API or network error (including 404).
        """
        params: Dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        data = self._get(
            f"/repos/{owner}/{repo}/contents/{path}", params=params,
        )
        if data.get("type") and data.get("type") != "file":
            return None
        content = data.get("content", "")
        encoding = data.get("encoding", "")
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8")
        return content

    def get_repo_tree(
        self,
        owner: str,
        repo: str,
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get the repository file tree at a given ref.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        params: Dict[str, Any] = {}
        if recursive:
            params["recursive"] = "1"
        data = self._get(
            f"/repos/{owner}/{repo}/git/trees/{ref}", params=params,
        )
        return data.get("tree", [])

    def get_rate_limit(self) -> Dict[str, Any]:
        """Check the current rate-limit status.

        Returns:
            The full rate-limit payload from the API.

        Raises:
            GitHubAPIError: On any API or network error.
        """
        return self._get("/rate_limit")

    # Convenience alias exposed at module level.
    parse_github_url = staticmethod(parse_github_url)
