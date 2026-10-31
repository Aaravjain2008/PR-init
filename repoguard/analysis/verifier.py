"""Claim verification against repository source code and documentation.

This module cross-references technical claims extracted from issues and PRs
with the actual repository contents.  Every piece of evidence is an
observable fact — no AI interpretation is produced here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from repoguard.models import (
    Claim,
    ClaimVerdict,
    Confidence,
    Evidence,
    EvidenceType,
    FindingCategory,
    ItemType,
)

logger = logging.getLogger(__name__)

# File patterns recognised as version-manifest files.
_VERSION_FILES: List[str] = [
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "version.txt",
    "VERSION",
]

# Documentation files checked for cross-referencing.
_DOC_FILES: List[str] = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
]

# Regex for file-path-like tokens (e.g. ``src/foo/bar.py``).
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`\"'(])([a-zA-Z0-9_.][a-zA-Z0-9_./\\-]*\.[a-zA-Z0-9]+)"
)

# Regex for code symbols:
# 1. Backtick identifiers: `foo_bar` or `FooBar`
# 2. PascalCase / CamelCase with internal capital: e.g. TokenStream, MyParser
# 3. snake_case with an underscore: e.g. parse_tokens, my_function
# 4. Function call syntax: e.g. parse(), run_check()
# 5. Dotted access: e.g. module.function, self.item
_SYMBOL_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_.]*)`"
    r"|"
    r"\b([A-Z][a-z0-9]+(?:[A-Z][a-zA-Z0-9]*)+)\b"
    r"|"
    r"\b([a-z_][a-z0-9]*_[a-z0-9_]+)\b"
    r"|"
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\(\)"
    r"|"
    r"\b([a-zA-Z_][a-zA-Z0-9_]+\.[a-zA-Z_][a-zA-Z0-9_]+)\b"
)

# Regex for a version string such as ``1.2.3`` or ``v2.0.0``.
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9._-]*)?)\b")

# Regex for line-number references (e.g. ``line 42``, ``L42``, ``#L42``).
_LINE_REF_RE = re.compile(
    r"(?:line\s*#?\s*|L|#L)(\d+)", re.IGNORECASE
)

# Simple stack-trace frame pattern (Python-style).
_STACK_FRAME_RE = re.compile(
    r'File "([^"]+)",\s*line\s+(\d+)'
)

# Common words / extensions / standard exceptions to skip when searching for symbols.
_SYMBOL_SKIP: Set[str] = {
    "None", "True", "False", "This", "The", "Also", "Error", "Warning",
    "Exception", "Not", "For", "With", "That", "Has", "Does", "Was",
    "Are", "All", "Any", "Its", "Can", "May", "Will", "Use", "Used",
    "Using", "See", "Set", "Get", "New", "Old", "Add", "Run", "Fix",
    "Bug", "Test", "File", "Line", "Code", "Type", "List", "Dict",
    "Int", "Str", "Bool", "References", "When", "Calling",
    # Standard runtime exceptions (not custom symbols):
    "AttributeError", "TypeError", "ValueError", "KeyError", "IndexError",
    "RuntimeError", "ImportError", "ModuleNotFoundError", "FileNotFoundError",
    "ZeroDivisionError", "StopIteration", "SyntaxError", "NameError",
    "MemoryError", "RecursionError", "AssertionError", "OSError",
}

# Extensions likely to contain code.
_CODE_EXTENSIONS: Set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java",
    ".rb", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt",
    ".scala", ".lua", ".sh", ".bash", ".zsh", ".toml", ".yaml",
    ".yml", ".json", ".cfg", ".ini", ".md", ".rst", ".txt",
}


class ClaimVerifier:
    """Verify technical claims against a repository's source code and docs.

    Parameters
    ----------
    github_client:
        An object with ``get_file_content``, ``get_repo_tree``, and
        ``get_pr_files`` methods (duck-typed).
    owner:
        Repository owner (user or organisation).
    repo:
        Repository name.
    ref:
        Git ref to operate against (branch, tag, SHA).  Defaults to HEAD.
    """

    def __init__(
        self,
        github_client: Any,
        owner: str,
        repo: str,
        ref: str = "HEAD",
    ) -> None:
        self._client = github_client
        self._owner = owner
        self._repo = repo
        self._ref = ref

        # Caches to avoid redundant API calls.
        self._tree_cache: Optional[List[Dict[str, str]]] = None
        self._file_cache: Dict[str, Optional[str]] = {}
        self._tree_paths: Optional[Set[str]] = None

    # ── public API ──────────────────────────────────────────────────────

    def verify_claims(
        self,
        claims: List[Claim],
        item_data: Optional[Dict[str, Any]] = None,
    ) -> List[Claim]:
        """Verify a list of claims and return them with updated evidence.

        Parameters
        ----------
        claims:
            The claims to verify.  Each claim's ``evidence`` list is mutated
            in-place *and* the updated claim is returned.
        item_data:
            Optional context dict that may contain ``number``, ``item_type``,
            ``body``, and ``diff_files``.
        """
        if not claims:
            return claims

        item_data = item_data or {}

        # Pre-fetch the repo tree once for the entire batch.
        self._ensure_tree()

        for claim in claims:
            self._gather_evidence(claim, item_data)

        # PR-level diff verification operates across all claims.
        if (
            item_data.get("item_type") == ItemType.PULL_REQUEST.value
            and item_data.get("diff_files")
        ):
            diff_evidence = self._verify_pr_diff(claims, item_data["diff_files"])
            # Attach diff evidence to the first claim that mentions a file,
            # or to every claim if none does.
            for ev in diff_evidence:
                attached = False
                for claim in claims:
                    if ev.file_path and ev.file_path in claim.text:
                        claim.evidence.append(ev)
                        attached = True
                        break
                if not attached and claims:
                    claims[0].evidence.append(ev)

        # Determine verdicts after all evidence is gathered.
        for claim in claims:
            self._determine_verdict(claim)

        return claims

    # ── evidence gathering ──────────────────────────────────────────────

    def _gather_evidence(
        self, claim: Claim, item_data: Dict[str, Any]
    ) -> None:
        """Run all applicable checks on *claim* and append evidence."""
        text = claim.text

        # 1. File existence
        for path in self._extract_file_paths(text):
            ev = self._check_file_exists(path)
            if ev is not None:
                claim.evidence.append(ev)

        # 2. Symbol search
        for symbol in self._extract_symbols(text):
            ev = self._search_symbol(symbol)
            if ev is not None:
                claim.evidence.append(ev)

        # 3. Line content
        for path, line_no, snippet in self._extract_line_refs(text):
            ev = self._check_line_content(path, line_no, snippet)
            if ev is not None:
                claim.evidence.append(ev)

        # 4. Stack trace
        if _STACK_FRAME_RE.search(text):
            for ev in self._validate_stack_trace(text):
                claim.evidence.append(ev)

        # 5. Version check
        for version in self._extract_versions(text):
            ev = self._check_version(version)
            if ev is not None:
                claim.evidence.append(ev)

        # 6. Documentation cross-reference
        for ev in self._check_documentation(text):
            claim.evidence.append(ev)

    # ── individual checks ───────────────────────────────────────────────

    def _check_file_exists(self, path: str) -> Optional[Evidence]:
        """Return evidence for whether *path* exists in the repo tree."""
        paths = self._get_tree_paths()
        normalised = path.lstrip("/")
        if normalised in paths:
            return Evidence(
                evidence_type=EvidenceType.FILE_EXISTS,
                category=FindingCategory.OBSERVED_FACT,
                description=f"File '{normalised}' exists in the repository.",
                file_path=normalised,
            )
        # Check suffix match (e.g. click/core.py in src/click/core.py)
        for p in paths:
            if p.endswith("/" + normalised) or p == normalised:
                return Evidence(
                    evidence_type=EvidenceType.FILE_EXISTS,
                    category=FindingCategory.OBSERVED_FACT,
                    description=f"File '{normalised}' found as '{p}' in the repository.",
                    file_path=p,
                )
        return Evidence(
            evidence_type=EvidenceType.FILE_NOT_FOUND,
            category=FindingCategory.OBSERVED_FACT,
            description=f"File '{normalised}' was not found in the repository.",
            file_path=normalised,
        )

    def _search_symbol(self, symbol: str) -> Optional[Evidence]:
        """Search code files for *symbol* (simple text match)."""
        # Determine candidate files to search.
        candidates = self._candidate_files_for_symbol(symbol)
        
        # Check for symbol or sub-symbols (e.g. Command.parse_args -> Command and parse_args)
        sym_variants = [symbol]
        if "." in symbol:
            parts = symbol.split(".")
            sym_variants.extend(parts)

        for fpath in candidates:
            content = self._get_file_content(fpath)
            if content is None:
                continue
            for var in sym_variants:
                if len(var) >= 3 and var in content:
                    # Find the first line that contains the symbol.
                    for idx, line in enumerate(content.splitlines(), start=1):
                        if var in line:
                            return Evidence(
                                evidence_type=EvidenceType.SYMBOL_FOUND,
                                category=FindingCategory.OBSERVED_FACT,
                                description=(
                                    f"Symbol '{symbol}' (matched '{var}') found in {fpath} "
                                    f"at line {idx}."
                                ),
                                file_path=fpath,
                                line_start=idx,
                                snippet=line.strip(),
                            )
        return Evidence(
            evidence_type=EvidenceType.SYMBOL_NOT_FOUND,
            category=FindingCategory.OBSERVED_FACT,
            description=(
                f"Symbol '{symbol}' was not found in any searched file."
            ),
        )

    def _check_line_content(
        self, path: str, line: int, expected: str
    ) -> Optional[Evidence]:
        """Check if the content at *line* in *path* matches *expected*."""
        content = self._get_file_content(path.lstrip("/"))
        if content is None:
            return Evidence(
                evidence_type=EvidenceType.FILE_NOT_FOUND,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Cannot verify line {line}: file '{path}' not found."
                ),
                file_path=path,
            )
        lines = content.splitlines()
        if line < 1 or line > len(lines):
            return Evidence(
                evidence_type=EvidenceType.LINE_MISMATCH,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Line {line} is out of range for '{path}' "
                    f"(file has {len(lines)} lines)."
                ),
                file_path=path,
                line_start=line,
            )
        actual = lines[line - 1]
        if expected.strip():
            if expected.strip() in actual:
                return Evidence(
                    evidence_type=EvidenceType.LINE_MATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Line {line} in '{path}' matches expected content."
                    ),
                    file_path=path,
                    line_start=line,
                    snippet=actual.strip(),
                )
            return Evidence(
                evidence_type=EvidenceType.LINE_MISMATCH,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Line {line} in '{path}' does not match expected content. "
                    f"Actual: {actual.strip()!r}"
                ),
                file_path=path,
                line_start=line,
                snippet=actual.strip(),
            )
        # Line exists within valid file bounds
        return Evidence(
            evidence_type=EvidenceType.LINE_MATCH,
            category=FindingCategory.OBSERVED_FACT,
            description=(
                f"Line {line} in '{path}' exists (line {line} of {len(lines)})."
            ),
            file_path=path,
            line_start=line,
            snippet=actual.strip(),
        )

    def _validate_stack_trace(self, trace: str) -> List[Evidence]:
        """Validate file/line references within a Python-style stack trace."""
        evidence: List[Evidence] = []
        for match in _STACK_FRAME_RE.finditer(trace):
            fpath = match.group(1)
            line_no = int(match.group(2))

            # Normalise absolute-looking paths to repo-relative.
            normalised = self._normalise_trace_path(fpath)

            paths = self._get_tree_paths()
            if normalised not in paths:
                evidence.append(Evidence(
                    evidence_type=EvidenceType.STACK_TRACE_MISMATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Stack trace file '{normalised}' not found in repo."
                    ),
                    file_path=normalised,
                    line_start=line_no,
                ))
                continue

            content = self._get_file_content(normalised)
            if content is None:
                evidence.append(Evidence(
                    evidence_type=EvidenceType.STACK_TRACE_MISMATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Could not fetch content for '{normalised}'."
                    ),
                    file_path=normalised,
                    line_start=line_no,
                ))
                continue

            total_lines = len(content.splitlines())
            if 1 <= line_no <= total_lines:
                evidence.append(Evidence(
                    evidence_type=EvidenceType.STACK_TRACE_MATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Stack trace reference '{normalised}' line {line_no} "
                        f"is valid ({total_lines} lines in file)."
                    ),
                    file_path=normalised,
                    line_start=line_no,
                ))
            else:
                evidence.append(Evidence(
                    evidence_type=EvidenceType.STACK_TRACE_MISMATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Stack trace line {line_no} out of range for "
                        f"'{normalised}' ({total_lines} lines)."
                    ),
                    file_path=normalised,
                    line_start=line_no,
                ))
        return evidence

    def _check_version(self, version: str) -> Optional[Evidence]:
        """Check version-manifest files for *version*."""
        paths = self._get_tree_paths()
        for vfile in _VERSION_FILES:
            if vfile not in paths:
                continue
            content = self._get_file_content(vfile)
            if content is None:
                continue
            if version in content:
                return Evidence(
                    evidence_type=EvidenceType.VERSION_CHECK,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Version '{version}' found in '{vfile}'."
                    ),
                    file_path=vfile,
                )
        # Version not found in any manifest.
        return Evidence(
            evidence_type=EvidenceType.VERSION_CHECK,
            category=FindingCategory.OBSERVED_FACT,
            description=(
                f"Version '{version}' was not found in any version-manifest "
                f"file ({', '.join(_VERSION_FILES)})."
            ),
        )

    def _check_documentation(self, claim_text: str) -> List[Evidence]:
        """Search documentation files for supporting/contradicting info."""
        evidence: List[Evidence] = []
        paths = self._get_tree_paths()

        doc_paths: List[str] = []
        for df in _DOC_FILES:
            if df in paths:
                doc_paths.append(df)
        # Also add files under docs/ directory.
        for p in paths:
            if p.startswith("docs/") and any(
                p.endswith(ext) for ext in (".md", ".rst", ".txt")
            ):
                doc_paths.append(p)

        # Extract significant words from the claim to search for.
        words = self._significant_words(claim_text)
        if not words:
            return evidence

        for doc_path in doc_paths:
            content = self._get_file_content(doc_path)
            if content is None:
                continue
            content_lower = content.lower()
            matched = [w for w in words if w.lower() in content_lower]
            if len(matched) >= 2 or (
                len(words) == 1 and len(matched) == 1
            ):
                evidence.append(Evidence(
                    evidence_type=EvidenceType.DOC_REFERENCE,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Documentation '{doc_path}' references: "
                        f"{', '.join(matched)}."
                    ),
                    file_path=doc_path,
                ))
        return evidence

    def _verify_pr_diff(
        self, claims: List[Claim], diff_files: List[Dict[str, Any]]
    ) -> List[Evidence]:
        """Cross-reference PR description claims with the actual diff."""
        evidence: List[Evidence] = []

        diff_filenames = {f["filename"] for f in diff_files}
        all_claim_text = " ".join(c.text for c in claims)

        # Check for mentioned files that actually appear in the diff.
        mentioned_in_claims: Set[str] = set()
        for fname in diff_filenames:
            if fname in all_claim_text:
                mentioned_in_claims.add(fname)
                evidence.append(Evidence(
                    evidence_type=EvidenceType.DIFF_MATCH,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"File '{fname}' is mentioned in claims and appears "
                        f"in the PR diff."
                    ),
                    file_path=fname,
                ))

        # Flag files changed but not mentioned.
        undescribed = diff_filenames - mentioned_in_claims
        for fname in sorted(undescribed):
            evidence.append(Evidence(
                evidence_type=EvidenceType.DIFF_MISMATCH,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"File '{fname}' was changed in the PR diff but is not "
                    f"mentioned in any claim."
                ),
                file_path=fname,
            ))

        # Check for files mentioned in claims that do NOT appear in the diff.
        for fpath in self._extract_file_paths(all_claim_text):
            normalised = fpath.lstrip("/")
            if normalised not in diff_filenames and normalised not in mentioned_in_claims:
                # Only flag if the file looks like it could be part of the
                # diff (i.e. it is a code file rather than e.g. a URL path).
                if any(normalised.endswith(ext) for ext in _CODE_EXTENSIONS):
                    evidence.append(Evidence(
                        evidence_type=EvidenceType.DIFF_MISMATCH,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"File '{normalised}' is mentioned in claims but "
                            f"does not appear in the PR diff."
                        ),
                        file_path=normalised,
                    ))

        return evidence

    # ── verdict determination ───────────────────────────────────────────

    def _determine_verdict(self, claim: Claim) -> Claim:
        """Set verdict, confidence, and reason on *claim* from its evidence."""
        if not claim.evidence:
            claim.verdict = ClaimVerdict.UNVERIFIABLE
            claim.confidence = Confidence.LOW
            claim.reason = "No verifiable evidence could be gathered."
            return claim

        supporting = []
        contradicting = []

        for ev in claim.evidence:
            if ev.evidence_type in _SUPPORTING_TYPES:
                supporting.append(ev)
            elif ev.evidence_type in _CONTRADICTING_TYPES:
                contradicting.append(ev)
            # Neutral types (VERSION_CHECK) need further inspection.
            elif ev.evidence_type == EvidenceType.VERSION_CHECK:
                if "not found" in ev.description.lower():
                    contradicting.append(ev)
                else:
                    supporting.append(ev)

        total = len(supporting) + len(contradicting)

        if total == 0:
            claim.verdict = ClaimVerdict.UNVERIFIABLE
            claim.confidence = Confidence.LOW
            claim.reason = "Evidence gathered but none conclusive."
            return claim

        if not contradicting:
            claim.verdict = ClaimVerdict.SUPPORTED
            claim.confidence = (
                Confidence.HIGH if len(supporting) >= 2 else Confidence.MEDIUM
            )
            claim.reason = (
                f"All {len(supporting)} piece(s) of evidence support "
                f"the claim."
            )
        elif not supporting:
            claim.verdict = ClaimVerdict.CONTRADICTED
            claim.confidence = (
                Confidence.HIGH if len(contradicting) >= 2 else Confidence.MEDIUM
            )
            claim.reason = (
                f"All {len(contradicting)} piece(s) of evidence contradict "
                f"the claim."
            )
        else:
            # Mixed evidence — weigh by count.
            if len(contradicting) > len(supporting):
                claim.verdict = ClaimVerdict.CONTRADICTED
                claim.confidence = Confidence.MEDIUM
            else:
                claim.verdict = ClaimVerdict.SUPPORTED
                claim.confidence = Confidence.LOW
            claim.reason = (
                f"{len(supporting)} supporting vs "
                f"{len(contradicting)} contradicting piece(s) of evidence."
            )

        return claim

    # ── helpers ─────────────────────────────────────────────────────────

    def _ensure_tree(self) -> None:
        """Fetch and cache the repo tree if not already cached."""
        if self._tree_cache is None:
            self._tree_cache = self._client.get_repo_tree(
                self._owner, self._repo, self._ref, recursive=True
            )
            self._tree_paths = {
                entry["path"]
                for entry in self._tree_cache
                if entry.get("type") in ("blob", "file", None)
            }

    def _get_tree_paths(self) -> Set[str]:
        """Return the set of file paths in the repo tree."""
        self._ensure_tree()
        assert self._tree_paths is not None
        return self._tree_paths

    def _get_file_content(self, path: str) -> Optional[str]:
        """Fetch file content with caching."""
        if path not in self._file_cache:
            self._file_cache[path] = self._client.get_file_content(
                self._owner, self._repo, path, self._ref
            )
        return self._file_cache[path]

    def _candidate_files_for_symbol(self, symbol: str) -> List[str]:
        """Return a prioritised list of files likely to contain *symbol*."""
        paths = self._get_tree_paths()
        candidates: List[str] = []
        for p in sorted(paths):
            if any(p.endswith(ext) for ext in _CODE_EXTENSIONS):
                candidates.append(p)
        return candidates

    @staticmethod
    def _extract_file_paths(text: str) -> List[str]:
        """Extract plausible file paths from *text*."""
        matches: List[str] = []
        for m in _FILE_PATH_RE.finditer(text):
            path = m.group(1).rstrip(".,;:!?)`'\"")
            # Filter out version-like strings and URLs.
            if re.match(r"^\d+\.\d+", path):
                continue
            ext = "." + path.split(".")[-1].lower() if "." in path else ""
            if ext in _CODE_EXTENSIONS or ("/" in path and len(ext) >= 2):
                matches.append(path)
        return matches

    @staticmethod
    def _extract_symbols(text: str) -> List[str]:
        """Extract identifiers (class/function names) from *text*."""
        symbols: List[str] = []
        seen: Set[str] = set()
        for m in _SYMBOL_RE.finditer(text):
            # Find the non-empty matching group
            sym = next((g for g in m.groups() if g), None)
            if not sym:
                continue
            sym = sym.rstrip(".,;:!?)`'\"()")
            # Skip if it is a file path with a known extension
            if any(sym.endswith(ext) for ext in _CODE_EXTENSIONS):
                continue
            if sym and sym not in seen and sym not in _SYMBOL_SKIP:
                if len(sym) >= 3 and not sym.isdigit():
                    seen.add(sym)
                    symbols.append(sym)
        return symbols

    @staticmethod
    def _extract_versions(text: str) -> List[str]:
        """Extract version strings from *text*."""
        return [m.group(1) for m in _VERSION_RE.finditer(text)]

    @staticmethod
    def _extract_line_refs(text: str) -> List[tuple]:
        """Extract ``(file, line_no, expected_snippet)`` tuples.

        This uses a simple heuristic: a file path followed by a line
        reference.  If no file is detected, the reference is skipped.
        """
        refs: List[tuple] = []
        file_paths = ClaimVerifier._extract_file_paths(text)
        line_matches = list(_LINE_REF_RE.finditer(text))

        if not file_paths or not line_matches:
            return refs

        # Pair each line reference with the nearest preceding file path.
        for lm in line_matches:
            line_no = int(lm.group(1))
            # Find the file path token closest before this line ref.
            best_path: Optional[str] = None
            best_dist = len(text)
            for fp in file_paths:
                idx = text.rfind(fp, 0, lm.start())
                if idx >= 0:
                    dist = lm.start() - idx
                    if dist < best_dist:
                        best_dist = dist
                        best_path = fp
            if best_path is not None:
                refs.append((best_path, line_no, ""))
        return refs

    @staticmethod
    def _normalise_trace_path(path: str) -> str:
        """Convert an absolute-looking trace path to repo-relative."""
        # Strip common prefixes like /home/user/project/ or
        # /usr/lib/python3.x/site-packages/.
        parts = path.replace("\\", "/").split("/")
        # Look for common project markers.
        for marker in ("src", "lib", "app", "tests", "test"):
            if marker in parts:
                idx = parts.index(marker)
                return "/".join(parts[idx:])
        # If the path starts with "/" just return the basename segments.
        if path.startswith("/"):
            return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        return path

    @staticmethod
    def _significant_words(text: str) -> List[str]:
        """Extract words from *text* that are useful for doc searching."""
        stop = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "can", "shall",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "about", "between", "through", "during",
            "before", "after", "above", "below", "up", "down", "out",
            "off", "over", "under", "again", "further", "then", "once",
            "it", "its", "this", "that", "these", "those", "and", "but",
            "or", "nor", "not", "so", "if", "when", "while", "where",
            "how", "what", "which", "who", "whom", "why", "i", "me",
            "my", "we", "our", "you", "your", "he", "him", "his", "she",
            "her", "they", "them", "their",
        }
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", text)
        return [w for w in words if len(w) > 2 and w.lower() not in stop]


# ── evidence classification sets ────────────────────────────────────────

_SUPPORTING_TYPES: set = {
    EvidenceType.FILE_EXISTS,
    EvidenceType.SYMBOL_FOUND,
    EvidenceType.LINE_MATCH,
    EvidenceType.DOC_REFERENCE,
    EvidenceType.DIFF_MATCH,
    EvidenceType.STACK_TRACE_MATCH,
}

_CONTRADICTING_TYPES: set = {
    EvidenceType.FILE_NOT_FOUND,
    EvidenceType.SYMBOL_NOT_FOUND,
    EvidenceType.LINE_MISMATCH,
    EvidenceType.DOC_CONTRADICTION,
    EvidenceType.DIFF_MISMATCH,
    EvidenceType.STACK_TRACE_MISMATCH,
}
