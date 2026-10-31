"""Comprehensive tests for ClaimExtractor."""

from __future__ import annotations

import uuid

import pytest

from repoguard.analysis.extractor import ClaimExtractor
from repoguard.models import Claim, ClaimVerdict, Confidence


@pytest.fixture
def extractor() -> ClaimExtractor:
    return ClaimExtractor()


# ── File references ─────────────────────────────────────────────────────


class TestExtractFileReferences:
    """extract_file_references tests."""

    def test_backtick_simple(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references("Check `main.py` for the bug.")
        assert "main.py" in refs

    def test_backtick_path(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references(
            "See `src/module/file.js` for details."
        )
        assert "src/module/file.js" in refs

    def test_bare_path(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references(
            "The problem is in src/utils/helper.py somewhere."
        )
        assert "src/utils/helper.py" in refs

    def test_simple_file(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references("Edit config.yaml to fix this.")
        assert "config.yaml" in refs

    def test_complex_nested_path(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references(
            "Update `lib/core/internals/data-manager.ts`."
        )
        assert "lib/core/internals/data-manager.ts" in refs

    def test_multiple_files(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references(
            "Both `foo.py` and `bar.js` are affected."
        )
        assert "foo.py" in refs
        assert "bar.js" in refs

    def test_no_false_positive_version(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references("Use version 1.2")
        # "1.2" should NOT appear as a file ref
        assert all(not r.startswith("1.") for r in refs)

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_file_references("") == []
        assert extractor.extract_file_references(None) == []  # type: ignore[arg-type]

    def test_deduplication(self, extractor: ClaimExtractor) -> None:
        refs = extractor.extract_file_references(
            "`app.py` and again `app.py`"
        )
        assert refs.count("app.py") == 1


# ── Symbol references ───────────────────────────────────────────────────


class TestExtractSymbolReferences:
    """extract_symbol_references tests."""

    def test_function_with_parens(self, extractor: ClaimExtractor) -> None:
        syms = extractor.extract_symbol_references("Call `process_data()`.")
        assert "process_data()" in syms

    def test_method_with_module(self, extractor: ClaimExtractor) -> None:
        syms = extractor.extract_symbol_references("Use `os.path.join()` instead.")
        assert "os.path.join()" in syms

    def test_pascal_case_class(self, extractor: ClaimExtractor) -> None:
        syms = extractor.extract_symbol_references(
            "The MyClassName class is broken."
        )
        assert "MyClassName" in syms

    def test_dotted_symbol_backtick(self, extractor: ClaimExtractor) -> None:
        syms = extractor.extract_symbol_references(
            "Call `module.function` for it."
        )
        assert "module.function" in syms

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_symbol_references("") == []

    def test_deduplication(self, extractor: ClaimExtractor) -> None:
        syms = extractor.extract_symbol_references(
            "`foo()` and `foo()` again"
        )
        assert syms.count("foo()") == 1


# ── Version references ──────────────────────────────────────────────────


class TestExtractVersionReferences:
    """extract_version_references tests."""

    def test_semver_v_prefix(self, extractor: ClaimExtractor) -> None:
        vers = extractor.extract_version_references("Updated in v1.2.3.")
        assert "1.2.3" in vers

    def test_version_keyword(self, extractor: ClaimExtractor) -> None:
        vers = extractor.extract_version_references("Use version 2.0.")
        assert "2.0" in vers

    def test_semver_prerelease(self, extractor: ClaimExtractor) -> None:
        vers = extractor.extract_version_references("Tag v3.0.0-beta.1 is out.")
        assert "3.0.0-beta.1" in vers

    def test_multiple_versions(self, extractor: ClaimExtractor) -> None:
        vers = extractor.extract_version_references(
            "Upgrade from v1.0 to v2.0.1."
        )
        assert "1.0" in vers
        assert "2.0.1" in vers

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_version_references("") == []


# ── Code blocks ─────────────────────────────────────────────────────────


class TestExtractCodeBlocks:
    """extract_code_blocks tests."""

    def test_simple_block(self, extractor: ClaimExtractor) -> None:
        text = "Example:\n```python\nprint('hello')\n```"
        blocks = extractor.extract_code_blocks(text)
        assert len(blocks) == 1
        assert "print('hello')" in blocks[0]

    def test_multiple_blocks(self, extractor: ClaimExtractor) -> None:
        text = "A:\n```\nfoo\n```\nB:\n```\nbar\n```"
        blocks = extractor.extract_code_blocks(text)
        assert len(blocks) == 2

    def test_empty_block_skipped(self, extractor: ClaimExtractor) -> None:
        text = "Nothing:\n```\n\n```"
        blocks = extractor.extract_code_blocks(text)
        assert blocks == []

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_code_blocks("") == []


# ── Stack traces ────────────────────────────────────────────────────────


class TestExtractStackTraces:
    """extract_stack_traces tests."""

    def test_python_traceback(self, extractor: ClaimExtractor) -> None:
        text = (
            "I got this error:\n"
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in main\n'
            "    do_stuff()\n"
            "  File \"lib.py\", line 5, in do_stuff\n"
            "    raise ValueError('oops')\n"
            "ValueError: oops\n"
        )
        traces = extractor.extract_stack_traces(text)
        assert len(traces) >= 1
        assert "Traceback" in traces[0]
        assert "ValueError" in traces[0]

    def test_js_error(self, extractor: ClaimExtractor) -> None:
        text = (
            "TypeError: Cannot read property 'foo' of undefined\n"
            "    at Object.<anonymous> (app.js:10:5)\n"
            "    at Module._compile (module.js:653:30)\n"
        )
        traces = extractor.extract_stack_traces(text)
        assert len(traces) >= 1
        assert "TypeError" in traces[0]

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_stack_traces("") == []

    def test_no_trace(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_stack_traces("Everything is fine.") == []


# ── Reproduction steps ──────────────────────────────────────────────────


class TestExtractReproductionSteps:
    """extract_reproduction_steps tests."""

    def test_numbered_steps(self, extractor: ClaimExtractor) -> None:
        text = (
            "Steps to reproduce:\n"
            "1. Clone the repo\n"
            "2. Run `python main.py`\n"
            "3. See the error\n"
        )
        repro = extractor.extract_reproduction_steps(text)
        assert repro is not None
        assert "Clone the repo" in repro
        assert "See the error" in repro

    def test_how_to_reproduce(self, extractor: ClaimExtractor) -> None:
        text = (
            "How to reproduce:\n"
            "1) Install the package\n"
            "2) Import it\n"
        )
        repro = extractor.extract_reproduction_steps(text)
        assert repro is not None
        assert "Install the package" in repro

    def test_no_repro_section(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_reproduction_steps("No steps here.") is None

    def test_empty_input(self, extractor: ClaimExtractor) -> None:
        assert extractor.extract_reproduction_steps("") is None
        assert extractor.extract_reproduction_steps(None) is None  # type: ignore[arg-type]


# ── PR fix references ───────────────────────────────────────────────────


class TestPRFixReferences:
    """PR fix/close/resolve claims."""

    def test_fixes(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("This PR fixes #42")
        texts = [c.text for c in claims]
        assert any("fix/close #42" in t for t in texts)

    def test_resolves(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("Resolves #100")
        texts = [c.text for c in claims]
        assert any("#100" in t for t in texts)

    def test_closes(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("Closes #7")
        texts = [c.text for c in claims]
        assert any("#7" in t for t in texts)


# ── extract_claims integration ──────────────────────────────────────────


class TestExtractClaims:
    """End-to-end extract_claims tests."""

    def test_empty_body(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("")
        assert claims == []

    def test_none_comments(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("body text", comments=None)
        # Should not raise
        assert isinstance(claims, list)

    def test_claim_fields(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("See `app.py` for the bug.")
        assert len(claims) >= 1
        c = claims[0]
        assert c.verdict == ClaimVerdict.UNVERIFIABLE
        assert c.confidence == Confidence.LOW
        assert c.source_location == "body"
        # claim_id is a valid UUID
        uuid.UUID(c.claim_id)

    def test_comment_location(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims(
            "", comments=["Check `utils.py`."]
        )
        assert any(c.source_location == "comment #1" for c in claims)

    def test_multiple_comments(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims(
            "",
            comments=["See `a.py`.", "See `b.py`."],
        )
        locs = {c.source_location for c in claims}
        assert "comment #1" in locs
        assert "comment #2" in locs

    def test_mixed_content(self, extractor: ClaimExtractor) -> None:
        """A realistic body with many claim types."""
        body = (
            "## Bug Report\n\n"
            "When running `process_data()` on Python 3.10, "
            "the file `src/parser/main.py` throws an error at line 42.\n\n"
            "```python\n"
            "result = process_data(input)\n"
            "```\n\n"
            "Steps to reproduce:\n"
            "1. Clone the repo\n"
            "2. Run `python main.py`\n"
            "3. Observe the crash\n\n"
            "This was working in v1.2.3 but broke in v2.0.0.\n\n"
            "Traceback (most recent call last):\n"
            '  File "main.py", line 42, in <module>\n'
            "    process_data(input)\n"
            "TypeError: expected str, got int\n\n"
            "This PR fixes #99.\n"
        )
        claims = extractor.extract_claims(body)
        texts = " ".join(c.text for c in claims)

        # File references
        assert any("src/parser/main.py" in c.text for c in claims)
        # Symbol references
        assert any("process_data()" in c.text for c in claims)
        # Version references
        assert any("1.2.3" in c.text for c in claims)
        assert any("2.0.0" in c.text for c in claims)
        # Stack trace
        assert any("stack trace" in c.text.lower() for c in claims)
        # Code block
        assert any("code block" in c.text.lower() for c in claims)
        # Repro steps
        assert any("reproduction" in c.text.lower() for c in claims)
        # PR fix
        assert any("#99" in c.text for c in claims)
        # Line reference
        assert any("line" in c.text.lower() and "42" in c.text for c in claims)
        # Environment
        assert any("Python" in c.text and "3.10" in c.text for c in claims)

        # All claims are UNVERIFIABLE
        assert all(c.verdict == ClaimVerdict.UNVERIFIABLE for c in claims)

    def test_environment_claims(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims("Tested on Ubuntu 22.04 with Node 18.0.")
        texts = " ".join(c.text for c in claims)
        assert "Ubuntu" in texts
        assert "Node" in texts

    def test_file_contains_claim(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims(
            "File utils.py contains the helper function."
        )
        texts = " ".join(c.text for c in claims)
        assert "contains" in texts.lower()

    def test_function_does_claim(self, extractor: ClaimExtractor) -> None:
        claims = extractor.extract_claims(
            "Function parse() returns None on failure."
        )
        texts = " ".join(c.text for c in claims)
        assert "returns" in texts.lower() or "does" in texts.lower()
