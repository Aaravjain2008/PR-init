"""Deterministic claim extraction from issue and PR text.

Extracts technical claims (file references, symbol references, versions,
stack traces, reproduction steps, etc.) using compiled regex patterns.
Each claim is returned as a :class:`~repoguard.models.Claim` with an
initial verdict of ``UNVERIFIABLE``.
"""

from __future__ import annotations

import re
import uuid
from typing import List, Optional

from repoguard.models import Claim, ClaimVerdict, Confidence


class ClaimExtractor:
    """Extract technical claims from issue / PR text using deterministic regex.

    All patterns are compiled at class level for performance.  Public helper
    methods (``extract_file_references``, ``extract_symbol_references``, etc.)
    can be called independently, but the main entry-point is
    :meth:`extract_claims`.
    """

    # ── Compiled patterns ───────────────────────────────────────────────

    # File references: word.ext or path/to/word.ext (optionally in backticks)
    _FILE_RE = re.compile(
        r'`(?P<bt>[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]{1,10})`'   # backtick-wrapped
        r'|(?<!\w)(?P<plain>'                                 # or bare path
        r'(?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]{1,10}'  # dir/file.ext
        r'|[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z]{1,10}'           # file.ext (no leading digit)
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

        Returns deduplicated list of file path strings.
        """
        if not text:
            return []
        refs: list[str] = []
        for m in self._FILE_RE.finditer(text):
            ref = m.group("bt") or m.group("plain")
            if ref and not self._is_version_like(ref):
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
        """Run all extractors on *text* and return claims."""
        claims: list[Claim] = []

        # File references
        for ref in self.extract_file_references(text):
            claims.append(self._make_claim(
                text=f"References file: {ref}",
                source_text=ref,
                location=location,
            ))

        # Symbol references
        for sym in self.extract_symbol_references(text):
            claims.append(self._make_claim(
                text=f"References symbol: {sym}",
                source_text=sym,
                location=location,
            ))

        # Line references
        for m in self._LINE_REF_RE.finditer(text):
            ref = m.group(1) or m.group(2)
            claims.append(self._make_claim(
                text=f"References line: {ref}",
                source_text=m.group(0),
                location=location,
            ))

        # Version references
        for ver in self.extract_version_references(text):
            claims.append(self._make_claim(
                text=f"References version: {ver}",
                source_text=ver,
                location=location,
            ))

        # Stack traces
        for trace in self.extract_stack_traces(text):
            claims.append(self._make_claim(
                text="Contains stack trace",
                source_text=trace,
                location=location,
            ))

        # Error/exception lines (only those NOT already inside a stack trace)
        traces_text = "\n".join(self.extract_stack_traces(text))
        for m in self._ERROR_RE.finditer(text):
            line = m.group(0).strip()
            if line not in traces_text:
                claims.append(self._make_claim(
                    text=f"Error reference: {line}",
                    source_text=line,
                    location=location,
                ))

        # Code blocks
        for block in self.extract_code_blocks(text):
            claims.append(self._make_claim(
                text="Contains code block",
                source_text=block,
                location=location,
            ))

        # Reproduction steps
        repro = self.extract_reproduction_steps(text)
        if repro:
            claims.append(self._make_claim(
                text="Contains reproduction steps",
                source_text=repro,
                location=location,
            ))

        # PR fix/close references
        for m in self._PR_FIX_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Claims to fix/close #{m.group(1)}",
                source_text=m.group(0),
                location=location,
            ))

        # Environment claims
        for m in self._ENV_RE.finditer(text):
            claims.append(self._make_claim(
                text=f"Environment: {m.group(1)} {m.group(2)}",
                source_text=m.group(0),
                location=location,
            ))

        # "File X contains Y" / "Function X does Y"
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

        return claims

    @staticmethod
    def _make_claim(
        text: str,
        source_text: str,
        location: str,
    ) -> Claim:
        """Create a ``Claim`` with default verdict ``UNVERIFIABLE``."""
        return Claim(
            claim_id=str(uuid.uuid4()),
            text=text,
            source_text=source_text,
            source_location=location,
            verdict=ClaimVerdict.UNVERIFIABLE,
            confidence=Confidence.LOW,
        )

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
