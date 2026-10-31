"""GitHub API client for RepoGuard.

Provides authenticated access to the GitHub REST API v3 for fetching
issues, pull requests, comments, file content, and repository trees.
"""

from repoguard.github.client import (  # noqa: F401
    GitHubAPIError,
    GitHubClient,
    parse_github_url,
)

__all__ = ["GitHubAPIError", "GitHubClient", "parse_github_url"]
