"""Data models for RepoGuard results and configuration.

This module defines the canonical data structures shared by the engine,
CLI, dashboard, and extension. All results flow through these models.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Schema version ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "1.0.0"


# ── Enums ───────────────────────────────────────────────────────────────────

class ClaimVerdict(str, Enum):
    """Verdict for an individual technical claim."""
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    UNVERIFIABLE = "unverifiable"
    MISSING_EVIDENCE = "missing_evidence"


class OverallAssessment(str, Enum):
    """Rolled-up assessment for an issue or PR."""
    NO_STRONG_CONCERN = "No strong concern found"
    REVIEW_RECOMMENDED = "Review recommended"
    INSUFFICIENT_EVIDENCE = "Insufficient evidence"


class Confidence(str, Enum):
    """Confidence level for a verdict or assessment."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScanStatus(str, Enum):
    """Status of a scan."""
    PENDING = "pending"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    ERROR = "error"
    PARTIAL = "partial"


class ItemType(str, Enum):
    """Type of GitHub item being analyzed."""
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


class EvidenceType(str, Enum):
    """Type of evidence supporting a verdict."""
    FILE_EXISTS = "file_exists"
    FILE_NOT_FOUND = "file_not_found"
    SYMBOL_FOUND = "symbol_found"
    SYMBOL_NOT_FOUND = "symbol_not_found"
    LINE_MATCH = "line_match"
    LINE_MISMATCH = "line_mismatch"
    CODE_ANALYSIS = "code_analysis"
    DOC_REFERENCE = "doc_reference"
    DOC_CONTRADICTION = "doc_contradiction"
    DIFF_MATCH = "diff_match"
    DIFF_MISMATCH = "diff_mismatch"
    SANDBOX_OUTPUT = "sandbox_output"
    VERSION_CHECK = "version_check"
    STACK_TRACE_MATCH = "stack_trace_match"
    STACK_TRACE_MISMATCH = "stack_trace_mismatch"
    MISSING_REPRODUCTION = "missing_reproduction"
    HEURISTIC = "heuristic"
    AI_INTERPRETATION = "ai_interpretation"


class FindingCategory(str, Enum):
    """Categories to keep separate per spec §4.5."""
    OBSERVED_FACT = "observed_fact"
    DETERMINISTIC_HEURISTIC = "deterministic_heuristic"
    AI_INTERPRETATION = "ai_interpretation"


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """A piece of evidence supporting a claim verdict."""
    evidence_type: EvidenceType
    category: FindingCategory
    description: str
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    url: Optional[str] = None
    snippet: Optional[str] = None
    sandbox_log: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence_type"] = self.evidence_type.value
        d["category"] = self.category.value
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Evidence:
        d = dict(d)
        d["evidence_type"] = EvidenceType(d["evidence_type"])
        d["category"] = FindingCategory(d["category"])
        return cls(**d)


@dataclass
class Claim:
    """An individual technical claim extracted from an issue or PR."""
    claim_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    source_text: str = ""  # Original text the claim was extracted from
    source_location: Optional[str] = None  # e.g., "body", "comment #3"
    verdict: ClaimVerdict = ClaimVerdict.UNVERIFIABLE
    confidence: Confidence = Confidence.LOW
    reason: str = ""
    evidence: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "source_text": self.source_text,
            "source_location": self.source_location,
            "verdict": self.verdict.value,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Claim:
        d = dict(d)
        d["verdict"] = ClaimVerdict(d.get("verdict", "unverifiable"))
        d["confidence"] = Confidence(d.get("confidence", "low"))
        d["evidence"] = [Evidence.from_dict(e) for e in d.get("evidence", [])]
        return cls(**d)


@dataclass
class ItemResult:
    """Analysis result for a single issue or PR."""
    item_type: ItemType
    number: int
    title: str
    author: str
    url: str
    created_at: str = ""
    body: str = ""
    claims: List[Claim] = field(default_factory=list)
    assessment: OverallAssessment = OverallAssessment.INSUFFICIENT_EVIDENCE
    assessment_confidence: Confidence = Confidence.LOW
    assessment_reason: str = ""
    ai_summary: Optional[str] = None  # Always labeled as AI-generated
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_type": self.item_type.value,
            "number": self.number,
            "title": self.title,
            "author": self.author,
            "url": self.url,
            "created_at": self.created_at,
            "body": self.body,
            "claims": [c.to_dict() for c in self.claims],
            "assessment": self.assessment.value,
            "assessment_confidence": self.assessment_confidence.value,
            "assessment_reason": self.assessment_reason,
            "ai_summary": self.ai_summary,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ItemResult:
        d = dict(d)
        d["item_type"] = ItemType(d["item_type"])
        d["assessment"] = OverallAssessment(d.get("assessment", "Insufficient evidence"))
        d["assessment_confidence"] = Confidence(d.get("assessment_confidence", "low"))
        d["claims"] = [Claim.from_dict(c) for c in d.get("claims", [])]
        return cls(**d)


@dataclass
class ScanResult:
    """Complete result of a RepoGuard scan."""
    scan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = SCHEMA_VERSION
    repository: str = ""  # owner/repo
    scan_status: ScanStatus = ScanStatus.PENDING
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: Optional[str] = None
    scope: str = ""  # e.g., "issue #42", "all open issues", "PR #17"
    items_found: int = 0
    items_scanned: int = 0
    items: List[ItemResult] = field(default_factory=list)
    api_limitations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    ollama_used: bool = False
    sandbox_used: bool = False
    sandbox_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
            "repository": self.repository,
            "scan_status": self.scan_status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scope": self.scope,
            "items_found": self.items_found,
            "items_scanned": self.items_scanned,
            "items": [i.to_dict() for i in self.items],
            "api_limitations": self.api_limitations,
            "warnings": self.warnings,
            "errors": self.errors,
            "ollama_used": self.ollama_used,
            "sandbox_used": self.sandbox_used,
            "sandbox_available": self.sandbox_available,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ScanResult:
        d = dict(d)
        d["scan_status"] = ScanStatus(d.get("scan_status", "pending"))
        d["items"] = [ItemResult.from_dict(i) for i in d.get("items", [])]
        return cls(**d)

    @classmethod
    def from_json(cls, s: str) -> ScanResult:
        return cls.from_dict(json.loads(s))


@dataclass
class EngineConfig:
    """Configuration for the RepoGuard engine."""
    github_token: Optional[str] = None
    ollama_endpoint: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    ollama_enabled: bool = False
    sandbox_enabled: bool = False
    sandbox_image: str = "python:3.11-slim"
    api_host: str = "127.0.0.1"
    api_port: int = 18329
    api_secret: Optional[str] = None  # Pairing secret for auth
    db_path: str = "repoguard.db"
    max_items_per_scan: int = 50
    request_timeout: int = 30

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Never serialize the token
        d.pop("github_token", None)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> EngineConfig:
        # Filter out unknown keys
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid_keys})

