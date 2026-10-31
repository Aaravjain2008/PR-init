"""Claim verification against repository source code and documentation.

This module cross-references technical claims extracted from issues and PRs
with the actual repository contents.  Every piece of evidence is an
observable fact — no AI interpretation is produced here.

Evidence rules (from the spec):
- A claim is SUPPORTED only by direct evidence: file exists, symbol found in
  code, line content matches, traceback frame matches, version found.
- A claim is CONTRADICTED only when direct evidence conflicts AND the claim
  explicitly refers to this repository's own files or modules.
- A claim is UNVERIFIABLE when it is about something outside the repository
  (stdlib, third-party packages, the reporter's own script) or needed data
  is unavailable.
- Generic word overlap with documentation is NOT evidence.
- One contradicting piece of direct evidence is NOT outvoted by many doc
  mentions.
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
from repoguard.analysis.extractor import STDLIB_MODULES

logger = logging.getLogger(__name__)

# File patterns recognised as version-manifest files.
# P5: expanded to cover more ecosystems.
_VERSION_FILES: List[str] = [
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "version.txt",
    "VERSION",
    # Additional version sources
    "configure.ac",
    "CMakeLists.txt",
    "pom.xml",
    "go.mod",
    "build.gradle",
    "meson.build",
    "__version__.py",
    "version.py",
    "VERSION.txt",
    "RELEASE",
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
        """Run all applicable checks on *claim* and append evidence.

        P2: Report-feature claims (stack trace present, code block, repro
        steps) are not verifiable and are left UNVERIFIABLE so they don't
        pollute the verdict tally.

        P3: Symbols extracted from stdlib / third-party packages or the
        reporter's own script are left UNVERIFIABLE, not CONTRADICTED.

        P4: Traceback-frame claims are verified against the repo tree;
        frames in site-packages or outside the repo are left unverifiable.
        """
        text = claim.text
        source_loc = claim.source_location or ""

        # ── P2: report-feature claims bypass verification ─────────────────
        if "[report_feature]" in source_loc:
            # These are facts about the report (has a stack trace, code block,
            # repro steps) — they are not verifiable claims. Leave UNVERIFIABLE
            # so they don't contribute to the supported/contradicted tally.
            claim.verdict = ClaimVerdict.UNVERIFIABLE
            claim.confidence = Confidence.LOW
            claim.reason = (
                "This is a report feature (stack trace/code block/repro "
                "steps present), not a verifiable claim."
            )
            return

        # ── P4: traceback exception / frame claims ────────────────────────
        if "[traceback_exception]" in source_loc:
            # The exception line itself comes from the traceback. We verify
            # that any stack frame pointing into this repo is valid; the
            # exception claim is unverifiable if all frames are outside the repo.
            trace_ev = self._validate_stack_trace(claim.source_text)
            if trace_ev:
                for ev in trace_ev:
                    claim.evidence.append(ev)
                self._determine_verdict(claim)
            else:
                claim.verdict = ClaimVerdict.UNVERIFIABLE
                claim.confidence = Confidence.LOW
                claim.reason = (
                    "Traceback frames point to files outside the repository "
                    "(e.g. user script or site-packages)."
                )
            return

        if "[traceback_frame]" in source_loc:
            # Extract the file/line from the claim text.
            m = re.search(r"Traceback frame: (.+?) line (\d+)", text)
            if m:
                fpath = m.group(1).strip()
                line_no = int(m.group(2))
                # Check if this frame is in site-packages / outside repo (P3/P4)
                if self._is_external_path(fpath):
                    claim.verdict = ClaimVerdict.UNVERIFIABLE
                    claim.confidence = Confidence.LOW
                    claim.reason = (
                        f"Frame '{fpath}' is in site-packages or outside the "
                        "repository; verdict is unverifiable."
                    )
                    return
                normalised = self._normalise_trace_path(fpath)
                paths = self._get_tree_paths()
                if normalised in paths or any(
                    p == normalised or p.endswith("/" + normalised)
                    for p in paths
                ):
                    ev = self._check_line_content(normalised, line_no, "")
                    if ev:
                        claim.evidence.append(ev)
                else:
                    claim.evidence.append(Evidence(
                        evidence_type=EvidenceType.STACK_TRACE_MISMATCH,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"Stack trace file '{normalised}' not found in repo."
                        ),
                        file_path=normalised,
                        line_start=line_no,
                    ))
            self._determine_verdict(claim)
            return

        # ── Line claims ───────────────────────────────────────────────────
        if text.startswith("References line:"):
            line_refs = self._extract_line_refs(text)
            if not line_refs:
                claim.verdict = ClaimVerdict.UNVERIFIABLE
                claim.confidence = Confidence.LOW
                claim.reason = "Line claim has no associated file; verdict is unverifiable."
                return
            for path, line_no, snippet in line_refs:
                if not snippet and claim.source_text:
                    token_m = re.match(
                        r'[^\(]*\(([^)]{1,40})\)', claim.source_text
                    )
                    if token_m:
                        snippet = token_m.group(1).strip()
                ev = self._check_line_content(path, line_no, snippet)
                if ev is not None:
                    claim.evidence.append(ev)
            self._determine_verdict(claim)
            return

        # 1. File existence
        for path in self._extract_file_paths(text):
            ev = self._check_file_exists(path)
            if ev is not None:
                claim.evidence.append(ev)

        # 2. Symbol search — P3: skip stdlib/third-party symbols
        for symbol in self._extract_symbols(text):
            if self._is_external_symbol(symbol):
                # Leave unverifiable; do not search or mark contradicted
                continue
            ev = self._search_symbol(symbol, claim_text=text)
            if ev is not None:
                claim.evidence.append(ev)

        # 3. Line content
        for path, line_no, snippet in self._extract_line_refs(text):
            # If no snippet yet, try to get one from source_text (e.g. "(def send)").
            if not snippet and claim.source_text:
                token_m = re.match(
                    r'[^\(]*\(([^)]{1,40})\)', claim.source_text
                )
                if token_m:
                    snippet = token_m.group(1).strip()
            ev = self._check_line_content(path, line_no, snippet)
            if ev is not None:
                claim.evidence.append(ev)

        # 4. Stack trace (legacy / JS style)
        if _STACK_FRAME_RE.search(text):
            for ev in self._validate_stack_trace(text):
                claim.evidence.append(ev)

        # 5. Version check (P5) - only for version claims, not environment claims
        if "version" in text.lower() and not text.startswith("Environment:"):
            for version in self._extract_versions(text):
                ev = self._check_version(version)
                if ev is not None:
                    claim.evidence.append(ev)

        # 6. Documentation cross-reference (P2: exact match only, not votes)
        for ev in self._check_documentation(text):
            claim.evidence.append(ev)

    # ── individual checks ───────────────────────────────────────────────

    def _check_file_exists(self, path: str) -> Optional[Evidence]:
        """Return evidence for whether *path* exists in the repo tree."""
        if self._is_external_path(path):
            return None
        paths = self._get_tree_paths()
        if not paths:
            return None
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
        if getattr(self, "_tree_truncated", False):
            return Evidence(
                evidence_type=EvidenceType.CODE_ANALYSIS,
                category=FindingCategory.OBSERVED_FACT,
                description=f"File '{normalised}' not found, but repository tree is truncated so absence cannot be confirmed.",
                file_path=normalised,
            )
        return Evidence(
            evidence_type=EvidenceType.FILE_NOT_FOUND,
            category=FindingCategory.OBSERVED_FACT,
            description=f"File '{normalised}' was not found in the repository.",
            file_path=normalised,
        )

    def _search_symbol(self, symbol: str, claim_text: str = "") -> Optional[Evidence]:
        """Search code files for *symbol* (simple text match)."""
        clean_symbol = symbol.rstrip("()")
        # Determine candidate files to search.
        candidates = self._candidate_files_for_symbol(clean_symbol)

        # Check for symbol variants
        parts = clean_symbol.split(".") if "." in clean_symbol else [clean_symbol]
        repo_pkg = self._repo.lower().replace("-", "_")
        paths = self._get_tree_paths()
        top_dirs = {p.split("/")[0].lower() for p in paths if "/" in p}
        is_pkg_qualified = len(parts) > 1 and (
            parts[0].lower() == repo_pkg
            or parts[0].lower() in top_dirs
            or any(p.lower().startswith(f"src/{parts[0].lower()}/") for p in paths)
        )
        is_dotted = len(parts) > 1

        searched = 0
        unavailable = 0

    def _search_symbol(self, symbol: str, claim_text: str = "") -> Optional[Evidence]:
        """Search code files for *symbol* (simple text match)."""
        clean_symbol = symbol.rstrip("()")
        # Determine candidate files to search.
        candidates = self._candidate_files_for_symbol(clean_symbol)

        parts = clean_symbol.split(".") if "." in clean_symbol else [clean_symbol]
        repo_pkg = self._repo.lower().replace("-", "_")
        paths = self._get_tree_paths()
        top_dirs = {p.split("/")[0].lower() for p in paths if "/" in p}
        is_pkg_qualified = len(parts) > 1 and (
            parts[0].lower() == repo_pkg
            or parts[0].lower() in top_dirs
            or any(p.lower().startswith(f"src/{parts[0].lower()}/") for p in paths)
        )
        is_dotted = len(parts) > 1

        searched = 0
        unavailable = 0

        if is_dotted:
            owner = parts[0]
            member = parts[-1]
            # Variants that constitute a real definition/use of member.
            member_def_variants = [
                f"def {member}",
                f"class {member}",
                f"{member} =",
                clean_symbol,           # full dotted name present verbatim
            ]
            if len(parts) > 2:
                member_def_variants.append(".".join(parts[1:]))
            member_ww_re = re.compile(r"\b" + re.escape(member) + r"\b")

            if is_pkg_qualified:
                # Owner is a package/directory name; it may not appear as a
                # literal token inside files.  Search for member anywhere in
                # the package's candidate files (already prioritised by
                # _candidate_files_for_symbol).
                for fpath in candidates:
                    content = self._get_file_content(fpath)
                    if content is None:
                        unavailable += 1
                        continue
                    searched += 1
                    lines = content.splitlines()
                    # Try definition variants first.
                    for var in member_def_variants:
                        if len(var) >= 3 and var in content:
                            for idx, line in enumerate(lines, start=1):
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
                    # Plain whole-word member fallback.
                    for idx, line in enumerate(lines, start=1):
                        if member_ww_re.search(line):
                            return Evidence(
                                evidence_type=EvidenceType.SYMBOL_FOUND,
                                category=FindingCategory.OBSERVED_FACT,
                                description=(
                                    f"Symbol '{symbol}' (matched '{member}') found in {fpath} "
                                    f"at line {idx}."
                                ),
                                file_path=fpath,
                                line_start=idx,
                                snippet=line.strip(),
                            )

                # Member not found anywhere in the package's files.
                if unavailable > 0:
                    return Evidence(
                        evidence_type=EvidenceType.CODE_ANALYSIS,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"Symbol not found in {searched} searched files; {unavailable} candidate "
                            f"files could not be read, so absence cannot be confirmed."
                        ),
                    )
                if searched > 0:
                    return Evidence(
                        evidence_type=EvidenceType.SYMBOL_NOT_FOUND,
                        category=FindingCategory.OBSERVED_FACT,
                        description=f"Symbol '{symbol}' was not found in repository.",
                    )
                return None

            else:
                # Dotted but NOT pkg-qualified: e.g. Session.nonexistent_method.
                # Owner is a class/module name that should appear literally in files.
                # Require member to be found in a file that also contains Owner.
                owner_ww_re = re.compile(r"\b" + re.escape(owner) + r"\b")
                owner_found_file: Optional[str] = None
                owner_found_line: Optional[int] = None

                for fpath in candidates:
                    content = self._get_file_content(fpath)
                    if content is None:
                        unavailable += 1
                        continue
                    searched += 1
                    lines = content.splitlines()

                    owner_in_file = bool(owner_ww_re.search(content))
                    if not owner_in_file:
                        continue

                    # Owner present; now look for member.
                    for var in member_def_variants:
                        if len(var) >= 3 and var in content:
                            for idx, line in enumerate(lines, start=1):
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
                    for idx, line in enumerate(lines, start=1):
                        if member_ww_re.search(line):
                            return Evidence(
                                evidence_type=EvidenceType.SYMBOL_FOUND,
                                category=FindingCategory.OBSERVED_FACT,
                                description=(
                                    f"Symbol '{symbol}' (matched '{member}') found in {fpath} "
                                    f"at line {idx}."
                                ),
                                file_path=fpath,
                                line_start=idx,
                                snippet=line.strip(),
                            )

                    # Owner found but member missing in this file.
                    if owner_found_file is None:
                        for idx, line in enumerate(lines, start=1):
                            if owner_ww_re.search(line):
                                owner_found_file = fpath
                                owner_found_line = idx
                                break

                # Member not found in any file that has Owner.
                if unavailable > 0:
                    return Evidence(
                        evidence_type=EvidenceType.CODE_ANALYSIS,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"Symbol not found in {searched} searched files; {unavailable} candidate "
                            f"files could not be read, so absence cannot be confirmed."
                        ),
                    )

                if owner_found_file is not None:
                    # Owner exists in the repo but member does not — contradicted.
                    return Evidence(
                        evidence_type=EvidenceType.SYMBOL_NOT_FOUND,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"'{owner}' found in {owner_found_file} line {owner_found_line}, "
                            f"but '{member}' not found in any file containing '{owner}'."
                        ),
                    )

                # Owner itself not found either — fall through to P3 check.
                if searched > 0 and self._is_repo_symbol(clean_symbol, claim_text):
                    return Evidence(
                        evidence_type=EvidenceType.SYMBOL_NOT_FOUND,
                        category=FindingCategory.OBSERVED_FACT,
                        description=f"Symbol '{symbol}' was not found in repository.",
                    )
                return None

        # ── single (non-dotted) symbol ───────────────────────────────────
        sym_variants = [clean_symbol]

        for fpath in candidates:
            content = self._get_file_content(fpath)
            if content is None:
                unavailable += 1
                continue
            searched += 1
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

        if unavailable > 0:
            return Evidence(
                evidence_type=EvidenceType.CODE_ANALYSIS,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Symbol not found in {searched} searched files; {unavailable} candidate "
                    f"files could not be read, so absence cannot be confirmed."
                ),
            )

        # P3: Only claims that name this repository's own files or modules can be contradicted.
        if searched > 0 and self._is_repo_symbol(clean_symbol, claim_text):
            return Evidence(
                evidence_type=EvidenceType.SYMBOL_NOT_FOUND,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Symbol '{symbol}' was not found in repository."
                ),
            )
        return None

    def _check_line_content(
        self, path: str, line: int, expected: str
    ) -> Optional[Evidence]:
        """Check if the content at *line* in *path* matches *expected*."""
        norm_path = path.lstrip("/")
        if self._is_external_path(path) or self._is_external_path(norm_path):
            return None
        content = self._get_file_content(norm_path)
        if content is None:
            return None
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
            # Check on that line or within 2 lines
            start_idx = max(0, line - 1 - 2)
            end_idx = min(len(lines), line - 1 + 3)
            window_lines = lines[start_idx:end_idx]
            if any(expected.strip() in l for l in window_lines):
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

            # P4: Frames in site-packages or outside the repo are unverifiable
            if self._is_external_path(fpath):
                continue

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
                    evidence_type=EvidenceType.CODE_ANALYSIS,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Could not fetch content for '{normalised}', line {line_no} cannot be verified."
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
        """Check version-manifest files and repo tags/releases for *version*.

        P5: expanded version-file list + tags/releases.  If no version source
        can be read the result is UNVERIFIABLE (no evidence returned).  Only
        returns a contradicting evidence item when a version source was read
        AND tags were read and the version is not among them.  A version that
        differs from the current source but whose tags could not be read is
        left UNVERIFIABLE (may be an older release).
        """
        paths = self._get_tree_paths()
        readable_files: List[str] = []
        unavailable_count = 0

        # --- Manifest files (fixed list) ---
        for vfile in _VERSION_FILES:
            candidates = [p for p in paths if p == vfile or p.endswith("/" + vfile)]
            for cpath in candidates:
                content = self._get_file_content(cpath)
                if content is None:
                    unavailable_count += 1
                    continue
                readable_files.append(cpath)
                if version in content:
                    return Evidence(
                        evidence_type=EvidenceType.VERSION_CHECK,
                        category=FindingCategory.OBSERVED_FACT,
                        description=(
                            f"Version '{version}' found in '{cpath}'."
                        ),
                        file_path=cpath,
                    )

        # --- __version__ / version = / VERSION = in __init__.py, _version.py, version.py ---
        _VERSION_SOURCE_BASENAMES = ("__init__.py", "_version.py", "version.py")
        _VERSION_ATTRS = re.compile(
            r"""(?:^|\b)(?:__version__|version|VERSION)\s*=\s*['"]([^'"]+)['"]""",
            re.MULTILINE,
        )
        for p in sorted(paths):
            basename = p.split("/")[-1]
            if basename not in _VERSION_SOURCE_BASENAMES:
                continue
            # Only check top two directory levels (e.g. requests/__init__.py, __init__.py)
            depth = p.count("/")
            if depth > 1:
                continue
            content = self._get_file_content(p)
            if content is None:
                unavailable_count += 1
                continue
            if p not in readable_files:
                readable_files.append(p)
            # Direct substring match first (handles version in string literals)
            if version in content:
                return Evidence(
                    evidence_type=EvidenceType.VERSION_CHECK,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Version '{version}' found in '{p}'."
                    ),
                    file_path=p,
                )

        # --- Tags / releases ---
        tags_readable = False
        try:
            tags = self._client.get_tags(self._owner, self._repo)
            if tags is not None:
                tags_readable = True
                for tag in tags:
                    tag_name = tag.get("name", "")
                    if version in tag_name:
                        return Evidence(
                            evidence_type=EvidenceType.VERSION_CHECK,
                            category=FindingCategory.OBSERVED_FACT,
                            description=(
                                f"Version '{version}' matches repository tag '{tag_name}'."
                            ),
                        )
        except Exception:
            pass

        try:
            releases = self._client.get_releases(self._owner, self._repo)
            if releases is not None:
                tags_readable = True
                for rel in releases:
                    rel_tag = rel.get("tag_name", "")
                    if version in rel_tag:
                        return Evidence(
                            evidence_type=EvidenceType.VERSION_CHECK,
                            category=FindingCategory.OBSERVED_FACT,
                            description=(
                                f"Version '{version}' matches release '{rel_tag}'."
                            ),
                        )
        except Exception:
            pass

        # --- Verdict ---
        if not readable_files and unavailable_count == 0:
            # No version sources exist in this repo at all.
            return None  # unverifiable

        if not readable_files or unavailable_count > 0:
            # Some sources exist but could not be read — unverifiable.
            return None

        # Version sources were read but version not found.
        if tags_readable:
            # Tags were read and version is not among them — contradicted.
            return Evidence(
                evidence_type=EvidenceType.VERSION_CHECK,
                category=FindingCategory.OBSERVED_FACT,
                description=(
                    f"Version '{version}' was not found in any readable version "
                    f"file ({', '.join(readable_files)}) and does not match any "
                    f"known repository tag or release."
                ),
            )

        # Tags could not be read — may be an older release, leave unverifiable.
        return None

    def _check_documentation(self, claim_text: str) -> List[Evidence]:
        """Search documentation files for supporting/contradicting info.

        P2: A doc reference counts ONLY when it contains the exact symbol,
        path, or error text from the claim. Generic word matching is NOT evidence.
        """
        evidence: List[Evidence] = []
        paths = self._get_tree_paths()

        doc_paths: List[str] = []
        for df in _DOC_FILES:
            if df in paths:
                doc_paths.append(df)
        for p in paths:
            if p.startswith("docs/") and any(
                p.endswith(ext) for ext in (".md", ".rst", ".txt")
            ):
                doc_paths.append(p)

        targets: List[str] = []
        for fp in self._extract_file_paths(claim_text):
            targets.append(fp)
            targets.append(fp.split("/")[-1])
        for sym in self._extract_symbols(claim_text):
            clean_sym = sym.rstrip("()")
            if len(clean_sym) >= 3:
                targets.append(clean_sym)
        m_err = re.search(r'([A-Za-z]+Error:[^\n`]+)', claim_text)
        if m_err:
            targets.append(m_err.group(1).strip())

        targets = [t for t in set(targets) if len(t) >= 3]
        if not targets:
            return evidence

        for doc_path in doc_paths:
            content = self._get_file_content(doc_path)
            if content is None:
                continue
            matched = [t for t in targets if t in content]
            if matched:
                evidence.append(Evidence(
                    evidence_type=EvidenceType.DOC_REFERENCE,
                    category=FindingCategory.OBSERVED_FACT,
                    description=(
                        f"Documentation '{doc_path}' mentions exact term(s): "
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
        """Set verdict, confidence, and reason on *claim* from its evidence.

        P2 rules:
        - Direct evidence (file exists/missing, symbol found/missing, line match/mismatch,
          diff match/mismatch, stack trace match/mismatch) dominates.
        - One contradicting piece of direct evidence must not be outvoted by weak items.
        - Mixed strong evidence gives UNVERIFIABLE with an explanation, not a vote.
        """
        if not claim.evidence:
            claim.verdict = ClaimVerdict.UNVERIFIABLE
            claim.confidence = Confidence.LOW
            claim.reason = "No verifiable evidence could be gathered."
            return claim

        direct_supporting = []
        direct_contradicting = []
        weak_supporting = []
        weak_contradicting = []

        direct_supporting_types = {
            EvidenceType.FILE_EXISTS,
            EvidenceType.SYMBOL_FOUND,
            EvidenceType.LINE_MATCH,
            EvidenceType.DIFF_MATCH,
            EvidenceType.STACK_TRACE_MATCH,
            EvidenceType.SANDBOX_OUTPUT,
        }
        direct_contradicting_types = {
            EvidenceType.FILE_NOT_FOUND,
            EvidenceType.SYMBOL_NOT_FOUND,
            EvidenceType.LINE_MISMATCH,
            EvidenceType.DIFF_MISMATCH,
            EvidenceType.STACK_TRACE_MISMATCH,
        }

        for ev in claim.evidence:
            if ev.evidence_type in direct_supporting_types:
                direct_supporting.append(ev)
            elif ev.evidence_type in direct_contradicting_types:
                direct_contradicting.append(ev)
            elif ev.evidence_type == EvidenceType.DOC_REFERENCE:
                weak_supporting.append(ev)
            elif ev.evidence_type == EvidenceType.DOC_CONTRADICTION:
                weak_contradicting.append(ev)
            elif ev.evidence_type == EvidenceType.VERSION_CHECK:
                if "not found" in ev.description.lower():
                    direct_contradicting.append(ev)
                else:
                    direct_supporting.append(ev)

        # Mixed direct evidence -> UNVERIFIABLE (not a vote)
        if direct_supporting and direct_contradicting:
            claim.verdict = ClaimVerdict.UNVERIFIABLE
            claim.confidence = Confidence.LOW
            claim.reason = (
                f"Mixed direct evidence: {len(direct_supporting)} supporting vs "
                f"{len(direct_contradicting)} contradicting piece(s) of evidence."
            )
            return claim

        # Direct contradicting evidence dominates over any weak items
        if direct_contradicting:
            claim.verdict = ClaimVerdict.CONTRADICTED
            claim.confidence = (
                Confidence.HIGH if len(direct_contradicting) >= 2 else Confidence.MEDIUM
            )
            claim.reason = (
                f"Direct evidence contradicts the claim: {direct_contradicting[0].description}"
            )
            return claim

        # Direct supporting evidence
        if direct_supporting:
            claim.verdict = ClaimVerdict.SUPPORTED
            claim.confidence = (
                Confidence.HIGH if len(direct_supporting) >= 2 else Confidence.MEDIUM
            )
            claim.reason = (
                f"Direct evidence supports the claim: {direct_supporting[0].description}"
            )
            return claim

        # Only weak evidence
        if weak_contradicting and not weak_supporting:
            claim.verdict = ClaimVerdict.CONTRADICTED
            claim.confidence = Confidence.LOW
            claim.reason = f"Documentation contradicts the claim: {weak_contradicting[0].description}"
            return claim

        if weak_supporting and not weak_contradicting:
            claim.verdict = ClaimVerdict.SUPPORTED
            claim.confidence = Confidence.LOW
            claim.reason = f"Documentation references the claim: {weak_supporting[0].description}"
            return claim

        claim.verdict = ClaimVerdict.UNVERIFIABLE
        claim.confidence = Confidence.LOW
        claim.reason = "Evidence gathered but none conclusive."
        return claim

    # ── helpers ─────────────────────────────────────────────────────────

    def _ensure_tree(self) -> None:
        """Fetch and cache the repo tree if not already cached."""
        if self._tree_cache is None:
            self._tree_cache = self._client.get_repo_tree(
                self._owner, self._repo, self._ref, recursive=True
            )
            self._tree_truncated = False
            if isinstance(self._tree_cache, dict):
                self._tree_truncated = bool(self._tree_cache.get("truncated", False))
                entries = self._tree_cache.get("tree", [])
            elif isinstance(self._tree_cache, list):
                entries = self._tree_cache
            else:
                entries = []
            self._tree_paths = {
                entry["path"]
                for entry in entries
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
        clean = symbol.rstrip("()")
        parts = clean.split(".")
        pkg_hint = parts[0].lower() if len(parts) > 1 else ""

        paths = self._get_tree_paths()
        pkg_files: List[str] = []
        other_files: List[str] = []

        for p in sorted(paths):
            if any(p.endswith(ext) for ext in _CODE_EXTENSIONS):
                p_lower = p.lower()
                if pkg_hint and (
                    p_lower.startswith(f"{pkg_hint}/")
                    or p_lower.startswith(f"src/{pkg_hint}/")
                    or f"/{pkg_hint}/" in p_lower
                ):
                    pkg_files.append(p)
                else:
                    other_files.append(p)

        return pkg_files + other_files

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

        Handles two forms:
        - "References line: N (file: <path>)" — produced by the extractor when
          a file path was found near the line reference.
        - Inline text like "``path/file.py`` line 42 (token)" where the file
          path precedes or follows the line number within ~80 characters.
        If no file path can be associated, the tuple is omitted (caller leaves
        the claim UNVERIFIABLE).
        """
        refs: List[tuple] = []

        # Form 1: structured "References line: N (file: <path>)" claim text.
        structured = re.match(
            r"References line:\s*(\d+)\s*\(file:\s*(.+?)\)\s*$", text.strip()
        )
        if structured:
            line_no = int(structured.group(1))
            fpath = structured.group(2).strip()
            # Check for a token hint in source_text — not available here, so no snippet.
            return [(fpath, line_no, "")]

        file_paths = ClaimVerifier._extract_file_paths(text)
        line_matches = list(_LINE_REF_RE.finditer(text))

        if not file_paths or not line_matches:
            return refs

        for lm in line_matches:
            line_no = int(lm.group(1))
            best_path: Optional[str] = None
            best_dist = 81  # only pair within 80 characters

            # Prefer file path immediately before the line ref.
            for fp in file_paths:
                idx = text.rfind(fp, 0, lm.start())
                if idx >= 0:
                    dist = lm.start() - (idx + len(fp))
                    if dist < best_dist:
                        best_dist = dist
                        best_path = fp

            # Also try file path immediately after the line ref.
            for fp in file_paths:
                idx = text.find(fp, lm.end())
                if idx >= 0:
                    dist = idx - lm.end()
                    if dist < best_dist:
                        best_dist = dist
                        best_path = fp

            if best_path is not None:
                # Extract a short token in parentheses right after the number.
                token_m = re.match(
                    r'\s*\(([^)]{1,40})\)', text[lm.end():lm.end() + 45]
                )
                snippet = token_m.group(1).strip() if token_m else ""
                refs.append((best_path, line_no, snippet))
        return refs

    def _is_external_path(self, path: str) -> bool:
        """Check if a path points outside this repository.

        Paths in site-packages, .venv, virtual environments, system
        directories, or interactive shells are external.
        """
        p = path.replace("\\", "/").lower()
        if any(marker in p for marker in ("site-packages", ".venv", "virtualenv", "dist-packages", "node_modules", "python3.")):
            return True
        if p.startswith(("/usr/", "/opt/", "/etc/", "/var/", "/system/", "/library/", "/home/", "/users/", "c:/")):
            return True
        if p.startswith(("usr/", "opt/", "etc/", "var/", "system/", "library/", "home/", "users/")):
            return True
        if p in ("<module>", "<stdin>", "<string>", "test.py", "script.py", "run.py", "main.py", "app.py", "example.py") and not any(
            tp == path or tp.endswith("/" + path) for tp in self._get_tree_paths()
        ):
            return True
        return False

    def _is_external_symbol(self, symbol: str) -> bool:
        """Check if a symbol is from stdlib, third-party packages, or external."""
        clean = symbol.rstrip("()")
        if self._is_repo_symbol(clean):
            return False
        parts = clean.split(".")
        root_module = parts[0].lower()
        if root_module in STDLIB_MODULES:
            return True
        if clean in {
            "print", "len", "range", "open", "str", "int", "float", "dict", "list",
            "set", "tuple", "bool", "type", "isinstance", "issubclass", "hasattr",
            "getattr", "setattr", "super", "property", "staticmethod", "classmethod",
            "Exception", "ValueError", "TypeError", "AttributeError", "KeyError",
            "IndexError", "RuntimeError", "FileNotFoundError",
        }:
            return True
        return False

    def _is_repo_symbol(self, symbol: str, claim_text: str = "") -> bool:
        """Check whether a symbol or claim asserts that the symbol lives in this repository."""
        clean = symbol.rstrip("()")
        parts = clean.split(".")
        repo_pkg = self._repo.lower().replace("-", "_")
        if parts[0].lower() == repo_pkg:
            return True
        paths = self._get_tree_paths()
        top_dirs = {p.split("/")[0].lower() for p in paths if "/" in p}
        if parts[0].lower() in top_dirs:
            return True
        src_dirs = {p.split("/")[1].lower() for p in paths if p.startswith("src/") and p.count("/") >= 2}
        if parts[0].lower() in src_dirs:
            return True
        for p in paths:
            stem = p.split("/")[-1].split(".")[0]
            if stem and parts[0].lower() == stem.lower():
                return True
            if p in claim_text:
                return True
        return False

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
