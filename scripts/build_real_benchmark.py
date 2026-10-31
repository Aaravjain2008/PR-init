#!/usr/bin/env python3
"""Build a real-data evaluation benchmark by querying closed issues from GitHub.

Requires GITHUB_TOKEN environment variable. Never prints or logs the token.
Fetches real closed issues with verified maintainer labels, snapshots repo trees
and mentioned file contents at the commit closest to the filing date, and assigns
dev/test splits deterministically.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from repoguard.analysis.extractor import ClaimExtractor

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("repoguard.real_benchmark")

# Candidate repositories that use maintainer triage labels
CANDIDATE_REPOS = [
    "psf/requests",
    "pallets/flask",
    "encode/httpx",
    "urllib3/urllib3",
    "tiangolo/fastapi",
    "tornadoweb/tornado",
    "aio-libs/aiohttp",
    "django/django",
    "scikit-learn/scikit-learn",
    "numpy/numpy",
    "pandas-dev/pandas",
    "curl/curl",
]

# Strict triage label mappings expressing maintainer conclusions
STRICT_LABEL_MAPPINGS = {
    "confirmed": {
        "strict_aliases": ["confirmed", "status: confirmed"],
        "loose_aliases": ["bug", "type: bug"],
        "ground_truth_label": "confirmed",
        "expected_assessment": "No strong concern found",
    },
    "invalid": {
        "strict_aliases": ["invalid", "resolution: invalid"],
        "loose_aliases": ["wontfix", "status: invalid"],
        "ground_truth_label": "invalid",
        "expected_assessment": "Review recommended",
    },
    "cannot reproduce": {
        "strict_aliases": ["cannot reproduce", "cannot-reproduce", "unreproducible"],
        "loose_aliases": ["needs reproduction"],
        "ground_truth_label": "cannot reproduce",
        "expected_assessment": "Insufficient evidence",
    },
}


def get_label_mappings(loose_labels: bool = False) -> Dict[str, Dict[str, Any]]:
    """Build label mapping dictionary respecting strict vs loose aliases."""
    mappings = {}
    for cat, data in STRICT_LABEL_MAPPINGS.items():
        aliases = list(data["strict_aliases"])
        if loose_labels:
            aliases.extend(data["loose_aliases"])
        mappings[cat] = {
            "aliases": aliases,
            "ground_truth_label": data["ground_truth_label"],
            "expected_assessment": data["expected_assessment"],
        }
    return mappings


# Backwards-compatible default mapping
LABEL_MAPPINGS = get_label_mappings(loose_labels=False)


def assign_split(repo: str, number: int) -> str:
    """Deterministically assign dev or test split based on SHA256 of repo#number."""
    key = f"{repo}#{number}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    return "dev" if int(digest[:8], 16) % 2 == 0 else "test"


class GitHubBenchmarkBuilder:
    """Builder that queries GitHub API for real issues and repository trees."""

    def __init__(
        self,
        token: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        max_retries: int = 3,
        loose_labels: bool = False,
        allow_truncated: bool = False,
        pool_size: int = 90,
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.cache_dir = cache_dir or Path(".benchmark_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.loose_labels = loose_labels
        self.allow_truncated = allow_truncated
        self.pool_size = pool_size
        self.pool_sizes: Dict[str, int] = {}
        self.label_mappings = get_label_mappings(loose_labels=loose_labels)

    def _api_get(self, url: str, params: Optional[Dict[str, str]] = None) -> Any:
        """Make an authenticated GET request to GitHub API with disk caching and rate-limit handling."""
        if params:
            from urllib.parse import urlencode
            full_url = f"{url}?{urlencode(params)}"
        else:
            full_url = url

        cache_key = hashlib.sha256(full_url.encode("utf-8")).hexdigest()
        cache_file = self.cache_dir / f"{cache_key}.json"

        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "RepoGuard-Benchmark-Builder/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = Request(full_url, headers=headers)

        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=30) as resp:
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining and int(remaining) == 0:
                        reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
                        sleep_time = max(0, reset_time - int(time.time())) + 1
                        logger.warning(f"Rate limit reached. Sleeping {sleep_time}s...")
                        time.sleep(min(sleep_time, 60))

                    data = json.loads(resp.read().decode("utf-8"))
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    return data
            except HTTPError as e:
                if e.code == 403 and "rate limit" in str(e.reason).lower():
                    reset_time = int(e.headers.get("X-RateLimit-Reset", 0))
                    sleep_time = max(0, reset_time - int(time.time())) + 1
                    logger.warning(f"Rate limit exceeded (HTTP 403). Sleeping {sleep_time}s...")
                    time.sleep(min(sleep_time, 60))
                elif e.code in (500, 502, 503, 504) and attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.warning(f"GitHub API error {e.code} for {full_url}: {e.reason}")
                    return None
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.warning(f"Request failed for {full_url}: {e}")
                    return None
        return None

    def get_repo_labels(self, repo: str) -> List[str]:
        """Fetch label names used in this repository."""
        url = f"https://api.github.com/repos/{repo}/labels"
        data = self._api_get(url, {"per_page": "100"})
        if isinstance(data, list):
            return [l["name"].lower() for l in data if "name" in l]
        return []

    def get_closest_commit_sha(self, repo: str, filing_date: str) -> Optional[str]:
        """Fetch the commit SHA closest to the issue filing date."""
        url = f"https://api.github.com/repos/{repo}/commits"
        data = self._api_get(url, {"until": filing_date, "per_page": "1"})
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("sha")
        return None

    def get_repo_tree(self, repo: str, commit_sha: str) -> Tuple[List[Dict[str, str]], bool]:
        """Fetch git tree at specific commit SHA and return (tree_items, is_truncated)."""
        url = f"https://api.github.com/repos/{repo}/git/trees/{commit_sha}"
        data = self._api_get(url, {"recursive": "1"})
        if isinstance(data, dict) and "tree" in data:
            tree_items = [{"path": item["path"], "type": item["type"]} for item in data["tree"]]
            is_truncated = bool(data.get("truncated", False))
            return tree_items, is_truncated
        return [], False

    def fetch_closed_issues_by_label(self, repo: str, label: str, pool_size: int = 90) -> List[Dict[str, Any]]:
        """Query closed issues for a given repo and label using paging up to pool_size, excluding pull requests."""
        collected: List[Dict[str, Any]] = []
        page = 1
        max_pages = 3  # Up to 3 pages of up to 100
        while len(collected) < pool_size and page <= max_pages:
            url = f"https://api.github.com/repos/{repo}/issues"
            params = {
                "state": "closed",
                "labels": label,
                "per_page": "100",
                "page": str(page),
                "sort": "updated",
                "direction": "desc",
            }
            data = self._api_get(url, params)
            if not isinstance(data, list) or not data:
                break
            for issue in data:
                if issue.get("state") == "closed" and "pull_request" not in issue:
                    collected.append(issue)
                    if len(collected) >= pool_size:
                        break
            if len(data) < 100:
                break
            page += 1
        return collected

    def fetch_file_contents(
        self,
        repo: str,
        commit_sha: str,
        tree: List[Dict[str, str]],
        text_to_scan: str,
        max_files: int = 25,
        max_total_bytes: int = 300 * 1024,
    ) -> Dict[str, str]:
        """Fetch file contents for files mentioned in issue text plus fixed version/doc files."""
        tree_blobs = {item["path"]: item for item in tree if item.get("type") == "blob"}
        target_paths: List[str] = []

        # 1. Mentioned files in issue text
        extractor = ClaimExtractor()
        mentioned = extractor.extract_file_references(text_to_scan)
        for ref in mentioned:
            if ref in tree_blobs:
                target_paths.append(ref)
            else:
                ref_base = ref.split("/")[-1]
                for p in tree_blobs:
                    if p.endswith("/" + ref_base) or p == ref_base:
                        target_paths.append(p)

        # 2. Fixed set of project/version/doc files
        fixed_names = [
            "README.md", "README.rst", "README",
            "setup.py", "pyproject.toml", "package.json",
        ]
        for fixed in fixed_names:
            if fixed in tree_blobs:
                target_paths.append(fixed)
            for p in tree_blobs:
                if p.endswith("/" + fixed):
                    target_paths.append(p)

        # Package __init__.py files
        for p in tree_blobs:
            if p == "__init__.py" or (p.count("/") == 1 and p.endswith("/__init__.py")):
                target_paths.append(p)

        # 3. Symbol stem matches (e.g. requests.Session -> requests/sessions.py, session*.py)
        symbols = extractor.extract_symbol_references(text_to_scan)
        for sym in symbols:
            parts = sym.split(".")
            for part in parts:
                if len(part) >= 3:
                    part_lower = part.lower()
                    for p in tree_blobs:
                        stem = Path(p).stem.lower()
                        if stem == part_lower or stem == part_lower + "s" or stem.startswith(part_lower):
                            target_paths.append(p)

        # Deduplicate preserving order
        unique_paths = list(dict.fromkeys(target_paths))

        files: Dict[str, str] = {}
        total_bytes = 0

        for path in unique_paths:
            if len(files) >= max_files:
                break
            url = f"https://api.github.com/repos/{repo}/contents/{path}"
            data = self._api_get(url, {"ref": commit_sha})
            if isinstance(data, dict) and "content" in data:
                try:
                    encoding = data.get("encoding", "base64")
                    raw_bytes = base64.b64decode(data["content"]) if encoding == "base64" else data["content"].encode("utf-8")
                    if total_bytes + len(raw_bytes) > max_total_bytes:
                        break
                    files[path] = raw_bytes.decode("utf-8", errors="replace")
                    total_bytes += len(raw_bytes)
                except Exception as e:
                    logger.debug(f"Failed to decode file {path}: {e}")

        return files

    def build_benchmark(
        self,
        repos: Optional[List[str]] = None,
        target_count: int = 60,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Build a balanced dataset of real GitHub issues."""
        target_total = limit if limit is not None else target_count
        repos_to_use = repos or CANDIDATE_REPOS
        cases: List[Dict[str, Any]] = []

        self.skipped_stats = {"truncated": 0, "no_label": 0, "no_commit": 0}
        self.cases_per_repo: Dict[str, int] = {}
        self.cases_per_label: Dict[str, int] = {}

        logger.info(f"Building real benchmark (target: {target_total} cases from up to {len(repos_to_use)} repos)...")

        for repo in repos_to_use:
            if len(cases) >= target_total:
                break

            repo_labels = self.get_repo_labels(repo)
            if not repo_labels:
                self.skipped_stats["no_label"] += 1
                continue

            for category, mapping in self.label_mappings.items():
                if len(cases) >= target_total:
                    break

                matched_label = None
                for alias in mapping["aliases"]:
                    if alias.lower() in repo_labels:
                        matched_label = alias
                        break

                if not matched_label:
                    continue

                candidates = self.fetch_closed_issues_by_label(repo, matched_label, pool_size=self.pool_size)
                self.pool_sizes[f"{repo} [{matched_label}]"] = len(candidates)
                # Sort candidates deterministically by sha256 of repo#number
                candidates.sort(
                    key=lambda iss: hashlib.sha256(f"{repo}#{iss['number']}".encode("utf-8")).hexdigest()
                )

                for issue in candidates:
                    if len(cases) >= target_total:
                        break

                    num = issue["number"]
                    created_at = issue.get("created_at", "")
                    closed_at = issue.get("closed_at", "")
                    body = issue.get("body", "") or ""
                    title = issue.get("title", "")

                    commit_sha = self.get_closest_commit_sha(repo, created_at)
                    if not commit_sha:
                        self.skipped_stats["no_commit"] += 1
                        continue

                    tree, is_truncated = self.get_repo_tree(repo, commit_sha)
                    if is_truncated and not self.allow_truncated:
                        self.skipped_stats["truncated"] += 1
                        logger.info(f"Skipping {repo}#{num}: tree is truncated and --allow-truncated not set")
                        continue

                    # Fetch file contents for mentioned, fixed, and symbol-stem matched files
                    files = self.fetch_file_contents(
                        repo=repo,
                        commit_sha=commit_sha,
                        tree=tree,
                        text_to_scan=f"{title}\n{body}",
                    )

                    repo_slug = repo.replace("/", "-")
                    split = assign_split(repo, num)

                    case = {
                        "id": f"{repo_slug}-{num}",
                        "repository": repo,
                        "number": num,
                        "url": issue.get("html_url", f"https://github.com/{repo}/issues/{num}"),
                        "title": title,
                        "body": body,
                        "ground_truth_label": mapping["ground_truth_label"],
                        "expected_assessment": mapping["expected_assessment"],
                        "label_source": matched_label,
                        "split": split,
                        "filing_date": created_at,
                        "closed_date": closed_at,
                        "body_fetched_at": datetime.now(timezone.utc).isoformat(),
                        "body_fetch_note": "Issue body was fetched from GitHub API at body_fetched_at and reflects content as of fetch time.",
                        "commit_sha": commit_sha,
                        "tree_truncated": is_truncated,
                        "files_fetched": len(files),
                        "files_available_in_tree": len([item for item in tree if item.get("type") in ("blob", "file", None)]),
                        "selection_method": "Closed issue with maintainer triage label on public repository fetched from GitHub API (deterministic hash sampled)",
                        "synthetic": False,
                        "mock_repo_context": {
                            "tree": tree,
                            "files": files,
                        },
                    }
                    cases.append(case)
                    self.cases_per_repo[repo] = self.cases_per_repo.get(repo, 0) + 1
                    gt_label = mapping["ground_truth_label"]
                    self.cases_per_label[gt_label] = self.cases_per_label.get(gt_label, 0) + 1
                    logger.info(f"Added real case {case['id']} ({gt_label}, {split})")

        return cases


def print_summary(cases: List[Dict[str, Any]], builder: GitHubBenchmarkBuilder):
    """Print benchmark generation summary with statistics and warnings."""
    cases_per_repo = getattr(builder, "cases_per_repo", {})
    cases_per_label = getattr(builder, "cases_per_label", {})
    pool_sizes = getattr(builder, "pool_sizes", {})
    skipped = getattr(builder, "skipped_stats", {"truncated": 0, "no_label": 0, "no_commit": 0})

    print("=" * 60)
    print("Real GitHub Benchmark Generation Summary")
    print("=" * 60)
    print(f"Total cases saved: {len(cases)}")
    print("Cases per repository:")
    for r, count in cases_per_repo.items():
        print(f"  {r}: {count}")
    print("Cases per label:")
    for lbl, count in cases_per_label.items():
        print(f"  {lbl}: {count}")
    print("Issue pool sizes per repo and label:")
    for k, sz in pool_sizes.items():
        print(f"  {k}: {sz} candidates")
    total_fetched = sum(c.get("files_fetched", 0) for c in cases)
    total_avail = sum(c.get("files_available_in_tree", 0) for c in cases)
    print(f"File contents fetched: {total_fetched} total files across cases (available in trees: {total_avail})")
    print("Skipped cases:")
    print(f"  Truncated trees:     {skipped.get('truncated', 0)}")
    print(f"  No matching label:   {skipped.get('no_label', 0)}")
    print(f"  No commit SHA found: {skipped.get('no_commit', 0)}")

    for lbl in ["confirmed", "invalid", "cannot reproduce"]:
        count = cases_per_label.get(lbl, 0)
        if count < 10:
            print(f"[!] Warning: Label '{lbl}' has only {count} cases (< 10 target).")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Build real GitHub issue evaluation benchmark.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of cases to fetch (e.g. 10)")
    parser.add_argument("--output", type=str, default="evaluation/data/real_benchmark.json", help="Output path")
    parser.add_argument("--loose-labels", action="store_true", help="Include loose labels like 'bug', 'wontfix'")
    parser.add_argument("--allow-truncated", action="store_true", help="Allow cases with truncated git trees")
    parser.add_argument("--pool-size", type=int, default=90, help="Candidate pool size per repo/label to sample from (default: 90)")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("[!] GITHUB_TOKEN environment variable not set. Real benchmark building requires GitHub authentication.")
        print("    Export GITHUB_TOKEN=<token> and re-run.")
        sys.exit(1)

    builder = GitHubBenchmarkBuilder(
        token=token,
        loose_labels=args.loose_labels,
        allow_truncated=args.allow_truncated,
        pool_size=args.pool_size,
    )
    cases = builder.build_benchmark(limit=args.limit)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2)

    print(f"Successfully wrote {len(cases)} real benchmark cases to {out_path}")
    print_summary(cases, builder)


if __name__ == "__main__":
    main()
