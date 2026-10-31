"""RepoGuard Engine: the central orchestrator.

This is the source of truth for all analysis. The CLI, dashboard,
and extension all use this engine (directly or via the API).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from repoguard.models import (
    Claim, ClaimVerdict, Confidence, EngineConfig, Evidence, EvidenceType,
    FindingCategory, ItemResult, ItemType, OverallAssessment, ScanResult,
    ScanStatus, SCHEMA_VERSION,
)
from repoguard.github.client import GitHubClient, GitHubAPIError
from repoguard.analysis.extractor import ClaimExtractor
from repoguard.analysis.verifier import ClaimVerifier
from repoguard.store.database import RepoGuardDB
from repoguard.ollama.client import OllamaClient
from repoguard.sandbox.runner import SandboxRunner

logger = logging.getLogger(__name__)


class RepoGuardEngine:
    """Central engine that orchestrates all RepoGuard analysis.
    
    Responsibilities:
    - GitHub data retrieval
    - Claim extraction and verification
    - Sandbox orchestration
    - Optional AI integration
    - Result storage and retrieval
    - Status reporting
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()
        
        # Initialize components
        self.db = RepoGuardDB(self.config.db_path)
        self.db.open()
        self.github = GitHubClient(
            token=self.config.github_token,
            timeout=self.config.request_timeout,
        )
        self.extractor = ClaimExtractor()
        self.ollama = OllamaClient(
            endpoint=self.config.ollama_endpoint,
            model=self.config.ollama_model,
        )
        self.sandbox = SandboxRunner(
            image=self.config.sandbox_image,
            enabled=self.config.sandbox_enabled,
        )
        
        # Current scan tracking
        self._current_scan: Optional[ScanResult] = None

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return engine status for the API."""
        return {
            "status": "ready",
            "version": SCHEMA_VERSION,
            "ollama_available": self.ollama.is_available() if self.config.ollama_enabled else False,
            "ollama_enabled": self.config.ollama_enabled,
            "sandbox_available": self.sandbox.is_available(),
            "sandbox_enabled": self.config.sandbox_enabled,
            "current_scan": (
                self._current_scan.to_dict() if self._current_scan else None
            ),
            "api_rate_limit": self._get_rate_limit_safe(),
        }

    def _get_rate_limit_safe(self) -> Optional[Dict]:
        """Get GitHub rate limit info without failing."""
        try:
            return self.github.get_rate_limit()
        except Exception:
            return None

    # ── Configuration ───────────────────────────────────────────────────

    def get_config(self) -> Dict[str, Any]:
        """Get current config (excluding secrets)."""
        return self.config.to_dict()

    def update_config(self, updates: Dict[str, Any]):
        """Update configuration fields."""
        for key, value in updates.items():
            if key == "github_token":
                # Token is set but never serialized
                self.config.github_token = value
                self.github = GitHubClient(
                    token=value,
                    timeout=self.config.request_timeout,
                )
            elif hasattr(self.config, key):
                setattr(self.config, key, value)
        
        # Reinitialize components that depend on config
        if "ollama_endpoint" in updates or "ollama_model" in updates:
            self.ollama = OllamaClient(
                endpoint=self.config.ollama_endpoint,
                model=self.config.ollama_model,
            )
        if "sandbox_enabled" in updates or "sandbox_image" in updates:
            self.sandbox = SandboxRunner(
                image=self.config.sandbox_image,
                enabled=self.config.sandbox_enabled,
            )

    # ── Scanning ────────────────────────────────────────────────────────

    def scan_item(
        self,
        repository: str,
        item_type: str,
        number: int,
    ) -> ScanResult:
        """Scan a single issue or PR.
        
        Args:
            repository: "owner/repo" string
            item_type: "issue" or "pull_request"
            number: Issue or PR number
            
        Returns:
            ScanResult with all findings
        """
        owner, repo = self._parse_repo(repository)
        
        scan = ScanResult(
            repository=repository,
            scope=f"{item_type} #{number}",
            scan_status=ScanStatus.FETCHING,
            sandbox_available=self.sandbox.is_available(),
        )
        self._current_scan = scan
        self._save_scan(scan)
        
        try:
            # 1. Fetch item data
            logger.info(f"Fetching {item_type} #{number} from {repository}")
            scan.scan_status = ScanStatus.FETCHING
            self._save_scan(scan)
            
            if item_type == "pull_request":
                item_data = self.github.get_pull_request(owner, repo, number)
                diff_files = self.github.get_pr_files(owner, repo, number)
                item_data["diff_files"] = diff_files
            else:
                item_data = self.github.get_issue(owner, repo, number)
                item_data["diff_files"] = []

            # Fetch comments
            comments_data = self.github.get_comments(owner, repo, number)
            comment_texts = [c.get("body", "") for c in comments_data if c.get("body")]

            scan.items_found = 1

            # 2. Extract claims
            logger.info(f"Extracting claims from {item_type} #{number}")
            scan.scan_status = ScanStatus.EXTRACTING
            self._save_scan(scan)

            body = item_data.get("body", "") or ""
            claims = self.extractor.extract_claims(
                body=body,
                comments=comment_texts,
                item_type=item_type,
            )

            # 3. Verify claims
            logger.info(f"Verifying {len(claims)} claims against repository")
            scan.scan_status = ScanStatus.VERIFYING
            self._save_scan(scan)

            verifier = ClaimVerifier(
                github_client=self.github,
                owner=owner,
                repo=repo,
            )
            claims = verifier.verify_claims(
                claims=claims,
                item_data=item_data,
            )

            # 4. Build item result
            item_result = ItemResult(
                item_type=ItemType(item_type),
                number=number,
                title=item_data.get("title", ""),
                author=item_data.get("user", {}).get("login", "unknown"),
                url=item_data.get("html_url", ""),
                created_at=item_data.get("created_at", ""),
                body=body[:2000],  # Truncate for storage
                claims=claims,
            )

            # 5. Determine overall assessment
            self._assess_item(item_result)

            # 6. Optional AI summary
            if self.config.ollama_enabled and self.ollama.is_available():
                summary = self._generate_ai_summary(item_result)
                if summary:
                    item_result.ai_summary = summary
                    scan.ollama_used = True

            scan.items.append(item_result)
            scan.items_scanned = 1
            scan.scan_status = ScanStatus.COMPLETE
            scan.completed_at = datetime.now(timezone.utc).isoformat()
            
        except GitHubAPIError as e:
            scan.errors.append(f"GitHub API error: {str(e)}")
            scan.scan_status = ScanStatus.ERROR
            scan.completed_at = datetime.now(timezone.utc).isoformat()
            logger.error(f"Scan failed: {e}")
        except Exception as e:
            scan.errors.append(f"Scan error: {str(e)}")
            scan.scan_status = ScanStatus.ERROR
            scan.completed_at = datetime.now(timezone.utc).isoformat()
            logger.error(f"Scan failed: {e}", exc_info=True)
        finally:
            self._current_scan = None
            self._save_scan(scan)
        
        return scan

    def scan_repository(
        self,
        repository: str,
        item_type: Optional[str] = None,
        state: str = "open",
    ) -> ScanResult:
        """Scan multiple issues and/or PRs in a repository.
        
        Args:
            repository: "owner/repo" string
            item_type: "issue", "pull_request", or None for both
            state: "open", "closed", or "all"
            
        Returns:
            ScanResult with all findings
        """
        owner, repo = self._parse_repo(repository)
        if not item_type:
            scope = f"issues and pull requests ({state})"
        else:
            scope = f"{item_type}s ({state})"
        
        scan = ScanResult(
            repository=repository,
            scope=scope,
            scan_status=ScanStatus.FETCHING,
            sandbox_available=self.sandbox.is_available(),
        )
        self._current_scan = scan
        self._save_scan(scan)

        try:
            items_to_scan = []

            # Fetch issues
            if item_type in (None, "issue"):
                logger.info(f"Fetching issues from {repository}")
                issues = self.github.list_issues(owner, repo, state=state)
                for iss in issues[:self.config.max_items_per_scan]:
                    # GitHub API returns PRs in issue list; filter them
                    if "pull_request" not in iss:
                        items_to_scan.append(("issue", iss))

            # Fetch PRs
            if item_type in (None, "pull_request"):
                logger.info(f"Fetching pull requests from {repository}")
                prs = self.github.list_pull_requests(owner, repo, state=state)
                for pr in prs[:self.config.max_items_per_scan]:
                    items_to_scan.append(("pull_request", pr))

            scan.items_found = len(items_to_scan)
            self._save_scan(scan)

            # Set up verifier once for the repo
            verifier = ClaimVerifier(
                github_client=self.github,
                owner=owner,
                repo=repo,
            )

            # Process each item
            for idx, (itype, idata) in enumerate(items_to_scan):
                number = idata.get("number", 0)
                logger.info(
                    f"Processing {itype} #{number} "
                    f"({idx + 1}/{len(items_to_scan)})"
                )
                
                try:
                    # Fetch full item data if needed
                    if itype == "pull_request":
                        full_data = self.github.get_pull_request(owner, repo, number)
                        diff_files = self.github.get_pr_files(owner, repo, number)
                        full_data["diff_files"] = diff_files
                    else:
                        full_data = idata
                        full_data["diff_files"] = []

                    # Fetch comments
                    comments_data = self.github.get_comments(owner, repo, number)
                    comment_texts = [
                        c.get("body", "") for c in comments_data if c.get("body")
                    ]

                    # Extract and verify claims
                    scan.scan_status = ScanStatus.EXTRACTING
                    body = full_data.get("body", "") or ""
                    claims = self.extractor.extract_claims(
                        body=body,
                        comments=comment_texts,
                        item_type=itype,
                    )

                    scan.scan_status = ScanStatus.VERIFYING
                    claims = verifier.verify_claims(claims=claims, item_data=full_data)

                    item_result = ItemResult(
                        item_type=ItemType(itype),
                        number=number,
                        title=full_data.get("title", ""),
                        author=full_data.get("user", {}).get("login", "unknown"),
                        url=full_data.get("html_url", ""),
                        created_at=full_data.get("created_at", ""),
                        body=body[:2000],
                        claims=claims,
                    )
                    self._assess_item(item_result)

                    # Optional AI summary
                    if self.config.ollama_enabled and self.ollama.is_available():
                        summary = self._generate_ai_summary(item_result)
                        if summary:
                            item_result.ai_summary = summary
                            scan.ollama_used = True

                    scan.items.append(item_result)
                    scan.items_scanned += 1
                    self._save_scan(scan)

                except GitHubAPIError as e:
                    scan.warnings.append(
                        f"Failed to process {itype} #{number}: {str(e)}"
                    )
                    logger.warning(f"Failed to process {itype} #{number}: {e}")
                except Exception as e:
                    scan.warnings.append(
                        f"Error processing {itype} #{number}: {str(e)}"
                    )
                    logger.warning(f"Error processing {itype} #{number}: {e}")

            if scan.items_found == 0:
                scan.scan_status = ScanStatus.COMPLETE
                scan.warnings.append("No open issues or pull requests found")
            elif scan.items_scanned == 0:
                scan.scan_status = ScanStatus.ERROR
                if not scan.errors:
                    scan.errors.append("Items were found but none could be processed")
            elif scan.items_scanned < scan.items_found:
                scan.scan_status = ScanStatus.PARTIAL
            else:
                scan.scan_status = ScanStatus.COMPLETE
            scan.completed_at = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            scan.errors.append(f"Repository scan error: {str(e)}")
            scan.scan_status = ScanStatus.ERROR
            scan.completed_at = datetime.now(timezone.utc).isoformat()
            logger.error(f"Repository scan failed: {e}", exc_info=True)
        finally:
            self._current_scan = None
            self._save_scan(scan)

        return scan

    # ── Assessment ──────────────────────────────────────────────────────

    def _assess_item(self, item: ItemResult):
        """Determine overall assessment for an item based on claim verdicts."""
        if not item.claims:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = Confidence.LOW
            item.assessment_reason = "No verifiable claims could be extracted"
            return

        verdicts = [c.verdict for c in item.claims]
        
        supported = sum(1 for v in verdicts if v in (
            ClaimVerdict.SUPPORTED, ClaimVerdict.REPRODUCED
        ))
        contradicted = sum(1 for v in verdicts if v == ClaimVerdict.CONTRADICTED)
        unverifiable = sum(1 for v in verdicts if v in (
            ClaimVerdict.UNVERIFIABLE, ClaimVerdict.MISSING_EVIDENCE
        ))
        total = len(verdicts)

        if contradicted > 0:
            item.assessment = OverallAssessment.REVIEW_RECOMMENDED
            if contradicted / total > 0.5:
                item.assessment_confidence = Confidence.HIGH
            elif contradicted / total > 0.25:
                item.assessment_confidence = Confidence.MEDIUM
            else:
                item.assessment_confidence = Confidence.LOW
            item.assessment_reason = (
                f"{contradicted} of {total} claims contradicted by evidence"
            )
        elif unverifiable / total > 0.7:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = Confidence.MEDIUM
            item.assessment_reason = (
                f"{unverifiable} of {total} claims could not be verified"
            )
        elif supported / total > 0.5:
            item.assessment = OverallAssessment.NO_STRONG_CONCERN
            if supported / total > 0.8:
                item.assessment_confidence = Confidence.HIGH
            else:
                item.assessment_confidence = Confidence.MEDIUM
            item.assessment_reason = (
                f"{supported} of {total} claims supported by evidence"
            )
        else:
            item.assessment = OverallAssessment.INSUFFICIENT_EVIDENCE
            item.assessment_confidence = Confidence.LOW
            item.assessment_reason = (
                f"Mixed results: {supported} supported, "
                f"{contradicted} contradicted, {unverifiable} unverifiable "
                f"out of {total} claims"
            )

    # ── AI Summary ──────────────────────────────────────────────────────

    def _generate_ai_summary(self, item: ItemResult) -> Optional[str]:
        """Generate an AI summary of findings for an item."""
        claims_text = "\n".join(
            f"- {c.text} → {c.verdict.value} ({c.confidence.value}): {c.reason}"
            for c in item.claims
        )
        evidence_text = "\n".join(
            f"- [{e.evidence_type.value}] {e.description}"
            for c in item.claims
            for e in c.evidence
        )
        return self.ollama.summarize_findings(claims_text, evidence_text)

    # ── Storage ─────────────────────────────────────────────────────────

    def _save_scan(self, scan: ScanResult):
        """Save or update scan result in the database."""
        try:
            self.db.save_scan(scan)
        except Exception as e:
            logger.error(f"Failed to save scan: {e}")

    def list_scans(
        self,
        repository: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """List recent scans."""
        scans = self.db.list_scans(repository=repository, limit=limit)
        return [s.to_dict() for s in scans]

    def get_scan(self, scan_id: str) -> Optional[Dict]:
        """Get a scan result by ID."""
        scan = self.db.get_scan(scan_id)
        if scan:
            return scan.to_dict()
        return None

    def clear_data(self):
        """Clear all stored data."""
        self.db.clear_all()

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_repo(repository: str) -> tuple:
        """Parse 'owner/repo' into (owner, repo)."""
        parts = repository.strip().strip("/").split("/")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid repository format: '{repository}'. "
                f"Expected 'owner/repo'."
            )
        # Handle full URLs
        if "github.com" in repository:
            for i, part in enumerate(parts):
                if part == "github.com" and i + 2 < len(parts):
                    return parts[i + 1], parts[i + 2]
        return parts[-2], parts[-1]

