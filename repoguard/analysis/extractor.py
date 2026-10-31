"""Deterministic claim extraction from issue and PR text.

Extracts technical claims (file references, symbol references, versions,
stack traces, reproduction steps, etc.) using compiled regex patterns.
Each claim is returned as a :class:`~repoguard.models.Claim` with an
initial verdict of ``UNVERIFIABLE``.
"""

from __future__ import annotations

import re
import uuid
from typing import Dict, List, Optional, Set

from repoguard.models import Claim, ClaimVerdict, Confidence


# Allowlisted extensions that are real source/config/doc file types.
# Dotted code names like os.getenv, requests.get, session.request,
# timezone.utc must NOT match — they are symbols, not file paths.
_FILE_EXTENSIONS: Set[str] = {
    "py", "js", "ts", "jsx", "tsx", "c", "h", "cpp", "hpp", "cc",
    "go", "rs", "java", "rb", "cs", "swift", "kt", "scala", "lua",
    "md", "rst", "txt", "yml", "yaml", "json", "toml", "cfg", "ini",
    "sh", "bash", "zsh", "html", "css", "xml", "sql", "proto",
}

# Well-known domain suffixes — prevent treating `stackoverflow.com`,
# `api.weather`, `v1.history` etc. as file references.
_DOMAIN_SUFFIXES: Set[str] = {
    "com", "org", "net", "io", "dev", "app", "co", "gov", "edu",
    "uk", "de", "fr", "au", "ca", "jp", "cn", "ru",
}

# Python standard-library top-level module names (non-exhaustive but covers
# common ones that appear in user scripts).
STDLIB_MODULES: Set[str] = {
    "os", "sys", "re", "io", "abc", "ast", "csv", "gc", "ssl", "pty",
    "pwd", "dis", "cmd", "cgi", "cmp", "code", "copy", "enum", "fnmatch",
    "glob", "gzip", "hmac", "html", "http", "json", "math", "mmap",
    "array", "uuid", "time", "stat", "shlex", "shutil", "socket",
    "struct", "bz2", "zlib", "zipfile", "tarfile", "pickle", "shelve",
    "email", "ftplib", "smtplib", "imaplib", "poplib", "xmlrpc",
    "urllib", "urllib2", "urlparse", "logging", "argparse", "pathlib",
    "decimal", "fractions", "random", "string", "textwrap", "difflib",
    "hashlib", "secrets", "base64", "binascii", "codecs", "locale",
    "gettext", "getpass", "readline", "rlcompleter", "sqlite3",
    "threading", "multiprocessing", "concurrent", "asyncio", "signal",
    "traceback", "inspect", "typing", "types", "functools", "operator",
    "itertools", "collections", "heapq", "bisect", "weakref", "contextlib",
    "dataclasses", "unittest", "doctest", "pdb", "profile", "cProfile",
    "timeit", "datetime", "calendar", "timezone", "pprint", "warnings",
    "platform", "subprocess", "tempfile", "token", "tokenize", "parser",
    "compileall", "importlib", "pkgutil", "site", "sysconfig", "ctypes",
    # common third-party packages that are NOT in the repo
    "requests", "flask", "django", "sqlalchemy", "numpy", "pandas",
    "scipy", "sklearn", "tensorflow", "torch", "pytest", "setuptools",
    "pkg_resources", "dotenv", "pydantic", "attrs", "click", "aiohttp",
    "httpx", "boto3", "paramiko", "cryptography", "jwt", "yaml",
}


def _is_dotted_code_name(ref: str) -> bool:
    """Return True if ref looks like a dotted code name (os.getenv, requests.get).

    Specifically: `word.word` where the final segment is NOT in the file
    extension allowlist. Also returns True for domain names.
    """
    if "/" in ref:
        return False
    parts = ref.split(".")
    if len(parts) < 2:
        return False
    ext = parts[-1].lower()
    if ext in _DOMAIN_SUFFIXES:
        return True
    if ext not in _FILE_EXTENSIONS:
        return True
    first = parts[0].lower()
    if first in STDLIB_MODULES:
        return True
    return False


class ClaimExtractor:
    """Extract technical claims from issue / PR text using deterministic regex.

    All patterns are compiled at class level for performance.  Public helper
    methods (``extract_file_references``, ``extract_symbol_references``, etc.)
    can be called independently, but the main entry-point is
    :meth:`extract_claims`.
    """

    # ── Compiled patterns ───────────────────────────────────────────────

    _FILE_RE = re.compile(
        r'`(?P<bt>[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]{1,10})`'   # backtick-wrapped
        r'|(?<!\w)(?P<plain>'                                 # or bare path
        r'(?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]{1,10}'  # dir/file.ext
        r'|[a-zA-Z_][a-zA-Z0-9_-]*\.[a-zA-Z0-9]{1,10}'              # bare file.ext
        r')(?!\w)',
    )

    # Function / method references: word() or module.word()
    _FUNC_RE = re.compile(
        r'`(?P<bt>[a-zA-Z_][a-zA-Z0-9_.]*\(\))`'
        r'|(?<!\w)(?P<plain>[a-zA-Z_][a-zA-Z0-9_.]*\(\))(?!\w)',
    )

    # Class / symbol references (PascalCase, at least two capital humps)
    _CLASS_RE = re.compile(
        r'`(?P<bt>[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]*)+)`'
        r'|(?<![a-zA-Z0-9_])(?P<plain>[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]*)+)(?![a-zA-Z0-9_(])',
    )

    # Dotted symbol references: module.symbol (not file-like, no extension)
    _DOTTED_SYMBOL_RE = re.compile(
        r'`(?P<bt>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)`',
    )

    # Line references: "line 42", "L42", "lines 10-20"
    _LINE_REF_RE = re.compile(
        r'(?i)\blines?\s+(\d+(?:\s*[-–]\s*\d+)?)\b'
        r'|\bL(\d+(?:\s*[-–]\s*\d+)?)\b',
    )

    # Version references: v1.2.3, version 1.2, semver
    _VERSION_RE = re.compile(
        r'(?i)\bv(?:ersion\s+)?(\d+(?:\.\d+){1,3}(?:-[a-zA-Z0-9.]+)?)\b',
    )

    # Error / exception keywords (single line)
    _ERROR_RE = re.compile(
        r'(?i)^.*(?:error|exception|traceback|raise\s|fatal|panic)[:\s].*$',
        re.MULTILINE,
    )

    # Fenced code blocks
    _CODE_BLOCK_RE = re.compile(
        r'```[^\n]*\n(.*?)```',
        re.DOTALL,
    )

    # Stack trace blocks (Python-style and JS-style)
    _STACK_TRACE_PYTHON_RE = re.compile(
        r'(Traceback \(most recent call last\):[\s\S]*?(?:\n[A-Za-z]\w*(?:Error|Exception|Warning)[^\n]*))',
    )
    _STACK_TRACE_JS_RE = re.compile(
        r'((?:[A-Za-z]*Error[^\n]*\n)(?:\s+at\s+[^\n]+\n?)+)',
    )

    # Reproduction steps
    _REPRO_HEADER_RE = re.compile(
        r'(?i)(?:steps?\s+to\s+reproduce|how\s+to\s+reproduce|reproduction\s+steps?|to\s+reproduce)',
    )

    # PR fix/close references
    _PR_FIX_RE = re.compile(
        r'(?i)(?:fix(?:es)?|resolve[sd]?|close[sd]?)\s+#(\d+)',
    )

    # Environment claims: "Python 3.10", "Node 18", "Ubuntu 22.04", "macOS 13"
    _ENV_RE = re.compile(
        r'(?i)\b(Python|Node(?:\.?js)?|Ruby|Java|Go|Rust|PHP|'
        r'Ubuntu|Debian|Fedora|CentOS|macOS|Windows|Linux'
        r')\s+v?(\d+(?:\.\d+){0,3})\b',
    )

    # "File X contains Y" / "Function X does Y"
    _CONTAINS_RE = re.compile(
        r'(?i)\b(?:file|module)\s+`?([a-zA-Z0-9_./]+)`?\s+'
        r'(?:contains?|has|includes?)\s+(.+?)(?:\.|$)',
        re.MULTILINE,
    )
    _FUNCTION_DOES_RE = re.compile(
        r'(?i)\b(?:function|method|class)\s+`?([a-zA-Z0-9_.()]+)`?\s+'
        r'(?:does|returns?|raises?|throws?|calls?|creates?|handles?|performs?)\s+(.+?)(?:\.|$)',
        re.MULTILINE,
    )

    # ── Public helpers ──────────────────────────────────────────────────

    def extract_file_references(self, text: str) -> List[str]:
        """Extract file paths from *text*.

        Returns deduplicated list of file path strings.  Dotted code names
        (``os.getenv``, ``requests.get``, ``session.request``) and domain names
        (``stackoverflow.com``, ``api.weather``) are excluded.
        """
        if not text:
            return []
        refs: list[str] = []
        for m in self._FILE_RE.finditer(text):
            ref = m.group("bt") or m.group("plain")
            if not ref:
                continue
            if self._is_version_like(ref):
                continue
            if _is_dotted_code_name(ref):
                continue
            # Backtick refs: also require allowlisted extension
            ext = ref.rsplit(".", 1)[-1].lower() if "." in ref else ""
            if ext not in _FILE_EXTENSIONS:
                continue
            refs.append(ref)
        return list(dict.fromkeys(refs))  # dedupe, preserve order

    def extract_symbol_references(self, text: str) -> List[str]:
        """Extract function calls and PascalCase class names from *text*.

        Returns deduplicated list of symbol strings.
        """
        if not text:
            return []
        syms: list[str] = []
        # Functions: foo(), module.bar()
        for m in self._FUNC_RE.finditer(text):
            sym = m.group("bt") or m.group("plain")
            if sym:
                syms.append(sym)
        # Classes: PascalCase
        for m in self._CLASS_RE.finditer(text):
            sym = m.group("bt") or m.group("plain")
            if sym:
                syms.append(sym)
        # Dotted symbols in backticks: module.function
        for m in self._DOTTED_SYMBOL_RE.finditer(text):
            sym = m.group("bt")
            # Skip if it looks like a file path (has extension)
            if sym and not self._looks_like_file(sym):
                syms.append(sym)
        return list(dict.fromkeys(syms))

    def extract_version_references(self, text: str) -> List[str]:
        """Extract version strings from *text*.

        Matches ``v1.2.3``, ``version 1.2``, semver with pre-release tags.
        """
        if not text:
            return []
        versions: list[str] = []
        for m in self._VERSION_RE.finditer(text):
            versions.append(m.group(1))
        return list(dict.fromkeys(versions))

    def extract_code_blocks(self, text: str) -> List[str]:
        """Extract fenced code block contents from *text*."""
        if not text:
            return []
        blocks: list[str] = []
        for m in self._CODE_BLOCK_RE.finditer(text):
            content = m.group(1).strip()
            if content:
                blocks.append(content)
        return blocks

    def extract_stack_traces(self, text: str) -> List[str]:
        """Extract stack traces (Python and JS style) from *text*."""
        if not text:
            return []
        traces: list[str] = []
        for m in self._STACK_TRACE_PYTHON_RE.finditer(text):
            traces.append(m.group(1).strip())
        for m in self._STACK_TRACE_JS_RE.finditer(text):
            traces.append(m.group(1).strip())
        return traces

    def extract_reproduction_steps(self, text: str) -> Optional[str]:
        """Extract reproduction steps section from *text*.

        Returns the full reproduction-steps block (header + numbered items),
        or ``None`` if no such section is found.
        """
        if not text:
            return None
        m = self._REPRO_HEADER_RE.search(text)
        if not m:
            return None
        # Grab from the header to the end of consecutive numbered/bulleted lines
        after = text[m.start():]
        lines = after.split("\n")
        collected: list[str] = [lines[0]]
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                # Allow one blank line between steps
                collected.append(line)
                continue
            if re.match(r'^\d+[.)]\s+', stripped) or re.match(r'^[-*]\s+', stripped):
                collected.append(line)
            elif stripped.startswith("#") or self._REPRO_HEADER_RE.match(stripped):
                break  # new section
            else:
                # Non-list prose after the steps → stop
                # But only if we already collected at least one step
                if any(
                    re.match(r'^\d+[.)]\s+', l.strip())
                    for l in collected[1:]
                    if l.strip()
                ):
                    break
                collected.append(line)
        result = "\n".join(collected).strip()
        return result if result else None

    def extract_claims(
        self,
        body: str,
        comments: Optional[List[str]] = None,
        item_type: str = "issue",
    ) -> List[Claim]:
        """Extract all technical claims from issue/PR text.

        Parameters
        ----------
        body:
            The main body text of the issue or PR.
        comments:
            Optional list of comment bodies.
        item_type:
            ``"issue"`` or ``"pull_request"``.

        Returns
        -------
        List[Claim]
            Claims with initial verdict ``UNVERIFIABLE``.
        """
        claims: list[Claim] = []

        if body:
            claims.extend(self._extract_from_text(body, "body"))

        for idx, comment in enumerate(comments or []):
            if comment:
                loc = f"comment #{idx + 1}"
                claims.extend(self._extract_from_text(comment, loc))

        return claims

    # ── Private helpers ─────────────────────────────────────────────────

    def _extract_from_text(self, text: str, location: str) -> List[Claim]:
        """Run all extractors on *text* and return claims.

        NOTE on report-feature claims (P2):
        "Contains stack trace", "Contains code block", and "Contains
        reproduction steps" are *descriptive facts about the report*, not
        verifiable technical claims.  They are returned with a dedicated
        claim_type so the verifier can use them for missing-evidence checks
        without attempting to mark them supported/contradicted.

        NOTE on primary traceback claims (P4):
        When a Python traceback is found its exception type, message, and
        in-repo frames are extracted as a *primary* claim that is ranked
        first.  Frames in site-packages or outside the repo root are left
        unverifiable by the verifier.
        """
        claims: list[Claim] = []
        primary_claims: list[Claim] = []

        # ── P4: Primary traceback / exception claims ─────────────────────
        for trace in self.extract_stack_traces(text):
            parsed = self._parse_traceback(trace)
            if parsed:
                exc_type, exc_msg, frames = parsed
                primary_claims.append(self._make_claim(
                    text=(
                        f"Exception: {exc_type}: {exc_msg}"
                        if exc_msg else f"Exception: {exc_type}"
                    ),
                    source_text=trace,
                    location=location,
                    claim_type="traceback_exception",
                ))
                for frame_file, frame_line, frame_func in frames:
                    primary_claims.append(self._make_claim(
                        text=(
                            f"Traceback frame: {frame_file} line {frame_line}"
                            + (f" in {frame_func}" if frame_func else "")
                        ),
                        source_text=trace,
                        location=location,
                        claim_type="traceback_frame",
                    ))

        # ── File references ──────────────────────────────────────────────
        # Exclude text inside stack traces so traceback frame paths are not
        # duplicated as generic file reference claims.
        text_for_files = text
        for trace in self.extract_stack_traces(text):
            text_for_files = text_for_files.replace(trace, " ")

        for ref in self.extract_file_references(text_for_files):
            claims.append(self._make_claim(
                text=f"References file: {ref}",
                source_text=ref,
                location=location,
            ))

        # ── Symbol references ────────────────────────────────────────────
        for sym in self.extract_symbol_references(text):
            claims.append(self._make_claim(
                text=f"References symbol: {sym}",
                source_text=sym,
                location=location,
            ))

        # ── Line references ──────────────────────────────────────────────
        # Pre-compute file references with their positions for proximity pairing.
        _file_refs_pos = []
        for fm in self._FILE_RE.finditer(text):
            fref = fm.group("bt") or fm.group("plain")
            if not fref:
                continue
            if self._is_version_like(fref):
                continue
            if _is_dotted_code_name(fref):
                continue
            ext = fref.rsplit(".", 1)[-1].lower() if "." in fref else ""
            if ext not in _FILE_EXTENSIONS:
                continue
            _file_refs_pos.append((fm.start(), fm.end(), fref))

        for m in self._LINE_REF_RE.finditer(text):
            ref = m.group(1) or m.group(2)
            # Look for a file reference within 80 chars before or after this match.
            nearby_file = None
            best_dist = 81
            for fstart, fend, fref in _file_refs_pos:
                dist = min(abs(m.start() - fend), abs(fstart - m.end()))
                if dist < best_dist:
                    best_dist = dist
                    nearby_file = fref
            # Also extract a short token in parentheses right after the number.
            token_m = re.match(r'\s*\(([^)]{1,40})\)', text[m.end():m.end() + 45])
            token = token_m.group(1).strip() if token_m else ""
            claim_text = f"References line: {ref}"
            if nearby_file:
                claim_text += f" (file: {nearby_file})"
            source = m.group(0) + (f" ({token})" if token else "")
            claims.append(self._make_claim(
                text=claim_text,
                source_text=source,
                location=location,
            ))

        # ── Version references ───────────────────────────────────────────
        for ver in self.extract_version_references(text):
            claims.append(self._make_claim(
                text=f"References version: {ver}",
                source_text=ver,
                location=location,
            ))

        # ── Error/exception lines outside a stack trace ──────────────────
        traces_text = "\n".join(self.extract_stack_traces(text))
        for m in self._ERROR_RE.finditer(text):
            line = m.group(0).strip()
            if line not in traces_text:
                claims.append(self._make_claim(
                    text=f"Error reference: {line}",
                    source_text=line,
                    location=location,
                ))

        # ── Report features (P2): not verifiable claims ──────────────────
        for trace in self.extract_stack_traces(text):
            claims.append(self._make_claim(
                text="Contains stack trace",
                source_text=trace,
                location=location,
                claim_type="report_feature",
            ))

        for block in self.extract_code_blocks(text):
            claims.append(self._make_claim(
                text="Contains code block",
                source_text=block,
                location=location,
                claim_type="report_feature",
            ))

        repro = self.extract_reproduction_steps(text)
        if repro:
            claims.append(self._make_claim(
                text="Contains reproduction steps",
                source_text=repro,
                location=location,
                claim_type="report_feature",
            ))

        # ── PR fix/close references ──────────────────────────────────────
        for m in self._PR_FIX_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Claims to fix/close #{m.group(1)}",
                source_text=m.group(0),
                location=location,
            ))

        # ── Environment claims ───────────────────────────────────────────
        for m in self._ENV_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Environment: {m.group(1)} {m.group(2)}",
                source_text=m.group(0),
                location=location,
            ))

        # ── "File X contains Y" / "Function X does Y" ───────────────────
        for m in self._CONTAINS_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Claim: file {m.group(1)} contains {m.group(2).strip()}",
                source_text=m.group(0).strip(),
                location=location,
            ))
        for m in self._FUNCTION_DOES_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Claim: {m.group(1)} does {m.group(2).strip()}",
                source_text=m.group(0).strip(),
                location=location,
            ))

        # Primary claims (traceback/exception) come first
        return primary_claims + claims

    @staticmethod
    def _make_claim(
        text: str,
        source_text: str,
        location: str,
        claim_type: str = "generic",
    ) -> Claim:
        """Create a ``Claim`` with default verdict ``UNVERIFIABLE``."""
        c = Claim(
            claim_id=str(uuid.uuid4()),
            text=text,
            source_text=source_text,
            source_location=location,
            verdict=ClaimVerdict.UNVERIFIABLE,
            confidence=Confidence.LOW,
        )
        # Store claim_type in metadata dict (Claim has no dedicated field,
        # so we encode it in source_location as a prefix for the verifier).
        # Use a special prefix so the verifier can inspect it.
        if claim_type != "generic":
            c.source_location = f"[{claim_type}] {location}"
        return c

    @staticmethod
    def _is_version_like(ref: str) -> bool:
        """Return True if *ref* looks like a bare version number (e.g. ``1.2``)."""
        return bool(re.fullmatch(r'\d+(?:\.\d+)+', ref))

    @staticmethod
    def _looks_like_file(ref: str) -> bool:
        """Return True if *ref* looks like a file path with extension."""
        parts = ref.rsplit(".", 1)
        if len(parts) != 2:
            return False
        ext = parts[1]
        return bool(re.fullmatch(r'[a-zA-Z0-9]{1,10}', ext)) and "/" in ref or ext in {
            "py", "js", "ts", "jsx", "tsx", "rb", "go", "rs", "java",
            "c", "cpp", "h", "hpp", "cs", "md", "txt", "yml", "yaml",
            "json", "toml", "cfg", "ini", "sh", "bash", "html", "css",
        }

    # Regex to parse Python traceback frames: File "path", line N[, in func]
    _TRACE_FRAME_RE = re.compile(
        r'File "([^"]+)",\s*line\s+(\d+)(?:,\s*in\s+(\S+))?'
    )
    # Regex to match the final exception line in a Python traceback
    _TRACE_EXC_RE = re.compile(
        r'^([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception|Warning|Interrupt|Stop'
        r'|KeyboardInterrupt|SystemExit|GeneratorExit|BaseException))'
        r'[:\s](.*)$',
        re.MULTILINE,
    )

    @classmethod
    def _parse_traceback(
        cls, trace: str
    ) -> Optional[tuple]:
        """Parse a Python traceback into (exc_type, exc_msg, frames).

        frames is a list of (filepath, line_no, func_name) tuples.
        Returns None if the trace cannot be parsed as a Python traceback.
        """
        if "Traceback (most recent call last)" not in trace:
            return None
        frames = []
        for m in cls._TRACE_FRAME_RE.finditer(trace):
            filepath = m.group(1)
            line_no = int(m.group(2))
            func = m.group(3) or ""
            frames.append((filepath, line_no, func))
        # Find exception line (last non-blank line after all frames)
        exc_type = ""
        exc_msg = ""
        exc_match = None
        for exc_match in cls._TRACE_EXC_RE.finditer(trace):
            pass  # iterate to last match
        if exc_match:
            exc_type = exc_match.group(1)
            exc_msg = exc_match.group(2).strip()
        if not exc_type and not frames:
            return None
        return exc_type, exc_msg, frames

