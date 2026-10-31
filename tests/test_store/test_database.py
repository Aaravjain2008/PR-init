"""Comprehensive tests for repoguard.store.database.RepoGuardDB."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from repoguard.models import (
    Claim,
    ClaimVerdict,
    Confidence,
    EngineConfig,
    Evidence,
    EvidenceType,
    FindingCategory,
    ItemResult,
    ItemType,
    OverallAssessment,
    ScanResult,
    ScanStatus,
)
from repoguard.store.database import RepoGuardDB


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_evidence(**overrides) -> Evidence:
    """Create a sample Evidence instance."""
    defaults = dict(
        evidence_type=EvidenceType.FILE_EXISTS,
        category=FindingCategory.OBSERVED_FACT,
        description="Found requirements.txt",
        file_path="requirements.txt",
        line_start=1,
        line_end=5,
        url="https://github.com/owner/repo/blob/main/requirements.txt",
        snippet="flask==2.0.0",
    )
    defaults.update(overrides)
    return Evidence(**defaults)


def _make_claim(**overrides) -> Claim:
    """Create a sample Claim instance with one evidence piece."""
    defaults = dict(
        claim_id=str(uuid.uuid4()),
        text="Flask 2.0 is used",
        source_text="We use Flask 2.0",
        source_location="body",
        verdict=ClaimVerdict.SUPPORTED,
        confidence=Confidence.HIGH,
        reason="requirements.txt lists flask==2.0.0",
        evidence=[_make_evidence()],
    )
    defaults.update(overrides)
    return Claim(**defaults)


def _make_item(**overrides) -> ItemResult:
    """Create a sample ItemResult with one claim."""
    defaults = dict(
        item_type=ItemType.ISSUE,
        number=42,
        title="Bug: crash on startup",
        author="contributor",
        url="https://github.com/owner/repo/issues/42",
        created_at="2025-01-01T00:00:00+00:00",
        body="The app crashes immediately.",
        claims=[_make_claim()],
        assessment=OverallAssessment.REVIEW_RECOMMENDED,
        assessment_confidence=Confidence.MEDIUM,
        assessment_reason="Claim is supported by evidence.",
        ai_summary="The crash is confirmed by evidence.",
        warnings=["Rate limited during scan"],
    )
    defaults.update(overrides)
    return ItemResult(**defaults)


def _make_scan(**overrides) -> ScanResult:
    """Create a sample ScanResult with one item."""
    defaults = dict(
        scan_id=str(uuid.uuid4()),
        repository="owner/repo",
        scan_status=ScanStatus.COMPLETE,
        started_at="2025-06-01T12:00:00+00:00",
        completed_at="2025-06-01T12:05:00+00:00",
        scope="issue #42",
        items_found=1,
        items_scanned=1,
        items=[_make_item()],
        api_limitations=["Rate limit reached"],
        warnings=["Partial results"],
        errors=[],
        ollama_used=True,
        sandbox_used=False,
        sandbox_available=True,
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db() -> RepoGuardDB:
    """Yield an in-memory RepoGuardDB for testing."""
    with RepoGuardDB(":memory:") as database:
        yield database


# ── Test: open / close / context manager ─────────────────────────────────────


class TestLifecycle:
    """Tests for database connection lifecycle."""

    def test_context_manager_opens_and_closes(self) -> None:
        """Context manager opens the connection and closes it on exit."""
        db = RepoGuardDB(":memory:")
        assert db._conn is None
        with db:
            assert db._conn is not None
        assert db._conn is None

    def test_open_is_idempotent(self) -> None:
        """Calling open() twice does not create a second connection."""
        db = RepoGuardDB(":memory:")
        db.open()
        conn1 = db._conn
        db.open()
        assert db._conn is conn1
        db.close()

    def test_connection_property_raises_when_closed(self) -> None:
        """Accessing .connection before open() raises RuntimeError."""
        db = RepoGuardDB(":memory:")
        with pytest.raises(RuntimeError, match="not open"):
            _ = db.connection

    def test_schema_meta_created(self, db: RepoGuardDB) -> None:
        """Schema version is recorded in schema_meta."""
        row = db.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'db_schema_version'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "1"


# ── Test: save & load scans ─────────────────────────────────────────────────


class TestSaveScan:
    """Tests for saving and loading scan results."""

    def test_save_and_load_round_trip(self, db: RepoGuardDB) -> None:
        """A saved scan can be loaded back identically."""
        scan = _make_scan()
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.to_dict() == scan.to_dict()

    def test_load_nonexistent_returns_none(self, db: RepoGuardDB) -> None:
        """Loading a scan that doesn't exist returns None."""
        assert db.load_scan("nonexistent-id") is None

    def test_save_replaces_existing(self, db: RepoGuardDB) -> None:
        """Saving a scan with the same ID replaces the old one."""
        scan = _make_scan()
        db.save_scan(scan)

        scan.scope = "updated scope"
        scan.items = []
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.scope == "updated scope"
        assert loaded.items == []

    def test_save_scan_no_items(self, db: RepoGuardDB) -> None:
        """A scan with no items can be saved and loaded."""
        scan = _make_scan(items=[], items_found=0, items_scanned=0)
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.items == []

    def test_save_scan_multiple_items(self, db: RepoGuardDB) -> None:
        """A scan with multiple items is persisted correctly."""
        items = [
            _make_item(number=1, title="Issue 1"),
            _make_item(
                number=2,
                title="PR 2",
                item_type=ItemType.PULL_REQUEST,
                url="https://github.com/owner/repo/pull/2",
            ),
        ]
        scan = _make_scan(items=items, items_found=2, items_scanned=2)
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert len(loaded.items) == 2
        assert loaded.items[0].number == 1
        assert loaded.items[1].item_type == ItemType.PULL_REQUEST

    def test_save_scan_claim_no_evidence(self, db: RepoGuardDB) -> None:
        """A claim with no evidence pieces is stored correctly."""
        claim = _make_claim(evidence=[])
        item = _make_item(claims=[claim])
        scan = _make_scan(items=[item])
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.items[0].claims[0].evidence == []

    def test_save_scan_multiple_evidence(self, db: RepoGuardDB) -> None:
        """Multiple evidence pieces on a claim are preserved."""
        ev1 = _make_evidence(description="Evidence 1")
        ev2 = _make_evidence(
            description="Evidence 2",
            evidence_type=EvidenceType.SYMBOL_FOUND,
            category=FindingCategory.DETERMINISTIC_HEURISTIC,
            snippet="class Foo:",
        )
        claim = _make_claim(evidence=[ev1, ev2])
        item = _make_item(claims=[claim])
        scan = _make_scan(items=[item])
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        evidences = loaded.items[0].claims[0].evidence
        assert len(evidences) == 2
        assert evidences[0].description == "Evidence 1"
        assert evidences[1].evidence_type == EvidenceType.SYMBOL_FOUND

    def test_save_scan_preserves_optional_none_fields(
        self, db: RepoGuardDB
    ) -> None:
        """Optional None fields survive the round trip."""
        ev = Evidence(
            evidence_type=EvidenceType.CODE_ANALYSIS,
            category=FindingCategory.AI_INTERPRETATION,
            description="AI analysis of code",
            # All optional fields default to None
        )
        claim = _make_claim(evidence=[ev], source_location=None)
        item = _make_item(claims=[claim], ai_summary=None)
        scan = _make_scan(items=[item], completed_at=None)
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.completed_at is None
        assert loaded.items[0].ai_summary is None
        assert loaded.items[0].claims[0].source_location is None
        loaded_ev = loaded.items[0].claims[0].evidence[0]
        assert loaded_ev.file_path is None
        assert loaded_ev.line_start is None
        assert loaded_ev.snippet is None

    def test_save_scan_boolean_flags(self, db: RepoGuardDB) -> None:
        """Boolean flags (ollama_used, sandbox_used, etc.) roundtrip correctly."""
        scan = _make_scan(
            ollama_used=True,
            sandbox_used=True,
            sandbox_available=False,
        )
        db.save_scan(scan)
        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.ollama_used is True
        assert loaded.sandbox_used is True
        assert loaded.sandbox_available is False


# ── Test: list scans ─────────────────────────────────────────────────────────


class TestListScans:
    """Tests for listing and filtering scans."""

    def test_list_empty_db(self, db: RepoGuardDB) -> None:
        """Empty database returns an empty list."""
        assert db.list_scans() == []

    def test_list_returns_all(self, db: RepoGuardDB) -> None:
        """All scans are returned when no filter is applied."""
        s1 = _make_scan(started_at="2025-01-01T00:00:00+00:00")
        s2 = _make_scan(started_at="2025-02-01T00:00:00+00:00")
        db.save_scan(s1)
        db.save_scan(s2)
        result = db.list_scans()
        assert len(result) == 2

    def test_list_ordered_by_started_at_desc(self, db: RepoGuardDB) -> None:
        """Results are ordered newest-first."""
        s1 = _make_scan(started_at="2025-01-01T00:00:00+00:00")
        s2 = _make_scan(started_at="2025-06-01T00:00:00+00:00")
        db.save_scan(s1)
        db.save_scan(s2)
        result = db.list_scans()
        assert result[0].scan_id == s2.scan_id
        assert result[1].scan_id == s1.scan_id

    def test_filter_by_repository(self, db: RepoGuardDB) -> None:
        """Filtering by repository returns only matching scans."""
        s1 = _make_scan(repository="owner/repo-a")
        s2 = _make_scan(repository="owner/repo-b")
        db.save_scan(s1)
        db.save_scan(s2)
        result = db.list_scans(repository="owner/repo-a")
        assert len(result) == 1
        assert result[0].repository == "owner/repo-a"

    def test_filter_by_status(self, db: RepoGuardDB) -> None:
        """Filtering by status works correctly."""
        s1 = _make_scan(scan_status=ScanStatus.COMPLETE)
        s2 = _make_scan(scan_status=ScanStatus.ERROR)
        db.save_scan(s1)
        db.save_scan(s2)
        result = db.list_scans(status=ScanStatus.ERROR)
        assert len(result) == 1
        assert result[0].scan_status == ScanStatus.ERROR

    def test_filter_by_date_range(self, db: RepoGuardDB) -> None:
        """Filtering by since/until ISO dates narrows results."""
        s1 = _make_scan(started_at="2025-01-01T00:00:00+00:00")
        s2 = _make_scan(started_at="2025-06-01T00:00:00+00:00")
        s3 = _make_scan(started_at="2025-12-01T00:00:00+00:00")
        for s in [s1, s2, s3]:
            db.save_scan(s)

        result = db.list_scans(
            since="2025-03-01T00:00:00+00:00",
            until="2025-09-01T00:00:00+00:00",
        )
        assert len(result) == 1
        assert result[0].scan_id == s2.scan_id

    def test_filter_combined(self, db: RepoGuardDB) -> None:
        """Multiple filters can be combined."""
        s1 = _make_scan(
            repository="owner/repo",
            scan_status=ScanStatus.COMPLETE,
            started_at="2025-06-01T00:00:00+00:00",
        )
        s2 = _make_scan(
            repository="owner/repo",
            scan_status=ScanStatus.ERROR,
            started_at="2025-06-02T00:00:00+00:00",
        )
        s3 = _make_scan(
            repository="other/repo",
            scan_status=ScanStatus.COMPLETE,
            started_at="2025-06-01T00:00:00+00:00",
        )
        for s in [s1, s2, s3]:
            db.save_scan(s)

        result = db.list_scans(
            repository="owner/repo",
            status=ScanStatus.COMPLETE,
        )
        assert len(result) == 1
        assert result[0].scan_id == s1.scan_id

    def test_limit_and_offset(self, db: RepoGuardDB) -> None:
        """Limit and offset paginate the results."""
        for i in range(5):
            db.save_scan(
                _make_scan(started_at=f"2025-0{i + 1}-01T00:00:00+00:00")
            )

        page1 = db.list_scans(limit=2, offset=0)
        page2 = db.list_scans(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # No overlap
        ids1 = {s.scan_id for s in page1}
        ids2 = {s.scan_id for s in page2}
        assert ids1.isdisjoint(ids2)


# ── Test: delete ─────────────────────────────────────────────────────────────


class TestDelete:
    """Tests for delete_scan and clear_all."""

    def test_delete_existing_scan(self, db: RepoGuardDB) -> None:
        """Deleting an existing scan removes it and returns True."""
        scan = _make_scan()
        db.save_scan(scan)
        assert db.delete_scan(scan.scan_id) is True
        assert db.load_scan(scan.scan_id) is None

    def test_delete_nonexistent_returns_false(self, db: RepoGuardDB) -> None:
        """Deleting a non-existent scan returns False."""
        assert db.delete_scan("no-such-id") is False

    def test_delete_cascades_to_children(self, db: RepoGuardDB) -> None:
        """Deleting a scan cascades to items, claims, and evidence."""
        scan = _make_scan()
        db.save_scan(scan)
        db.delete_scan(scan.scan_id)

        conn = db.connection
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0

    def test_clear_all(self, db: RepoGuardDB) -> None:
        """clear_all removes scans and config."""
        db.save_scan(_make_scan())
        db.set_config(EngineConfig(ollama_model="mistral"))
        db.clear_all()

        assert db.list_scans() == []
        # Config is also cleared; get_config returns defaults.
        config = db.get_config()
        assert config.ollama_model == "llama3.2"  # default

    def test_clear_all_on_empty_db(self, db: RepoGuardDB) -> None:
        """clear_all on an empty database is a no-op."""
        db.clear_all()  # Should not raise
        assert db.list_scans() == []


# ── Test: configuration ─────────────────────────────────────────────────────


class TestConfig:
    """Tests for configuration get/set."""

    def test_get_config_default(self, db: RepoGuardDB) -> None:
        """get_config returns a default EngineConfig when nothing is stored."""
        config = db.get_config()
        assert isinstance(config, EngineConfig)
        assert config.ollama_endpoint == "http://localhost:11434"

    def test_set_and_get_config_round_trip(self, db: RepoGuardDB) -> None:
        """Configuration survives a round trip."""
        config = EngineConfig(
            ollama_model="codellama",
            ollama_enabled=True,
            sandbox_enabled=True,
            sandbox_image="python:3.12-slim",
            api_port=9999,
            max_items_per_scan=25,
            request_timeout=60,
        )
        db.set_config(config)

        loaded = db.get_config()
        assert loaded.ollama_model == "codellama"
        assert loaded.ollama_enabled is True
        assert loaded.sandbox_enabled is True
        assert loaded.sandbox_image == "python:3.12-slim"
        assert loaded.api_port == 9999
        assert loaded.max_items_per_scan == 25
        assert loaded.request_timeout == 60

    def test_set_config_never_stores_github_token(
        self, db: RepoGuardDB
    ) -> None:
        """github_token is never written to the database."""
        config = EngineConfig(github_token="ghp_supersecret123")
        db.set_config(config)

        # Check raw DB – no token row should exist.
        row = db.connection.execute(
            "SELECT * FROM config WHERE key = 'github_token'"
        ).fetchone()
        assert row is None

        loaded = db.get_config()
        assert loaded.github_token is None  # Default, not persisted

    def test_set_config_never_stores_api_secret(
        self, db: RepoGuardDB
    ) -> None:
        """api_secret is never written to the database."""
        config = EngineConfig(api_secret="s3cret")
        db.set_config(config)

        row = db.connection.execute(
            "SELECT * FROM config WHERE key = 'api_secret'"
        ).fetchone()
        assert row is None

    def test_set_config_replaces_previous(self, db: RepoGuardDB) -> None:
        """Setting config again replaces the old values."""
        db.set_config(EngineConfig(ollama_model="first"))
        db.set_config(EngineConfig(ollama_model="second"))
        loaded = db.get_config()
        assert loaded.ollama_model == "second"

    def test_get_config_value(self, db: RepoGuardDB) -> None:
        """get_config_value retrieves a single key."""
        db.set_config(EngineConfig(ollama_model="test-model"))
        val = db.get_config_value("ollama_model")
        assert val is not None
        assert json.loads(val) == "test-model"

    def test_get_config_value_nonexistent(self, db: RepoGuardDB) -> None:
        """get_config_value returns None for a missing key."""
        assert db.get_config_value("no_such_key") is None

    def test_get_config_value_sensitive_returns_none(
        self, db: RepoGuardDB
    ) -> None:
        """get_config_value returns None for sensitive keys."""
        assert db.get_config_value("github_token") is None

    def test_set_config_value(self, db: RepoGuardDB) -> None:
        """set_config_value stores a single key-value pair."""
        db.set_config_value("custom_key", json.dumps("custom_val"))
        val = db.get_config_value("custom_key")
        assert val is not None
        assert json.loads(val) == "custom_val"

    def test_set_config_value_rejects_sensitive_key(
        self, db: RepoGuardDB
    ) -> None:
        """set_config_value raises ValueError for sensitive keys."""
        with pytest.raises(ValueError, match="sensitive key"):
            db.set_config_value("github_token", "secret")


# ── Test: thread safety flag ─────────────────────────────────────────────────


class TestThreadSafety:
    """Verify the check_same_thread=False flag is set."""

    def test_check_same_thread_disabled(self) -> None:
        """The connection should be usable across threads."""
        import threading

        results: list = []

        with RepoGuardDB(":memory:") as db:
            db.save_scan(_make_scan())

            def worker() -> None:
                # Should not raise if check_same_thread=False
                scans = db.list_scans()
                results.append(len(scans))

            t = threading.Thread(target=worker)
            t.start()
            t.join()

        assert results == [1]


# ── Test: edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    """Tests for edge cases and special values."""

    def test_empty_strings(self, db: RepoGuardDB) -> None:
        """Empty string fields are preserved."""
        scan = _make_scan(
            scope="",
            items=[
                _make_item(
                    body="",
                    assessment_reason="",
                    created_at="",
                    claims=[
                        _make_claim(
                            text="",
                            source_text="",
                            reason="",
                        )
                    ],
                )
            ],
        )
        db.save_scan(scan)
        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.scope == ""
        assert loaded.items[0].body == ""
        assert loaded.items[0].claims[0].text == ""

    def test_unicode_content(self, db: RepoGuardDB) -> None:
        """Unicode text is stored and retrieved correctly."""
        scan = _make_scan(
            scope="テスト",
            items=[
                _make_item(
                    title="Bug: 日本語タイトル",
                    body="Описание на русском 🐛",
                    claims=[
                        _make_claim(
                            text="断言 #1",
                            evidence=[
                                _make_evidence(
                                    description="Доказательство",
                                    snippet="コードスニペット",
                                )
                            ],
                        )
                    ],
                )
            ],
        )
        db.save_scan(scan)
        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert loaded.scope == "テスト"
        assert loaded.items[0].title == "Bug: 日本語タイトル"
        assert loaded.items[0].body == "Описание на русском 🐛"
        assert loaded.items[0].claims[0].text == "断言 #1"

    def test_all_enum_values_round_trip(self, db: RepoGuardDB) -> None:
        """Every enum variant survives a save/load cycle."""
        for status in ScanStatus:
            sid = str(uuid.uuid4())
            scan = _make_scan(scan_id=sid, scan_status=status, items=[])
            db.save_scan(scan)
            loaded = db.load_scan(sid)
            assert loaded is not None
            assert loaded.scan_status == status

    def test_all_verdict_values(self, db: RepoGuardDB) -> None:
        """Every ClaimVerdict variant survives a round trip."""
        for verdict in ClaimVerdict:
            claim = _make_claim(
                claim_id=str(uuid.uuid4()), verdict=verdict, evidence=[]
            )
            item = _make_item(claims=[claim])
            scan = _make_scan(items=[item])
            db.save_scan(scan)
            loaded = db.load_scan(scan.scan_id)
            assert loaded is not None
            assert loaded.items[0].claims[0].verdict == verdict

    def test_all_evidence_types(self, db: RepoGuardDB) -> None:
        """Every EvidenceType variant survives a round trip."""
        for et in EvidenceType:
            ev = _make_evidence(evidence_type=et)
            claim = _make_claim(claim_id=str(uuid.uuid4()), evidence=[ev])
            item = _make_item(claims=[claim])
            scan = _make_scan(items=[item])
            db.save_scan(scan)
            loaded = db.load_scan(scan.scan_id)
            assert loaded is not None
            assert loaded.items[0].claims[0].evidence[0].evidence_type == et

    def test_large_scan(self, db: RepoGuardDB) -> None:
        """A scan with many items/claims doesn't break."""
        items = []
        for i in range(20):
            claims = [
                _make_claim(
                    claim_id=str(uuid.uuid4()),
                    evidence=[_make_evidence(), _make_evidence()],
                )
                for _ in range(5)
            ]
            items.append(
                _make_item(
                    number=i,
                    title=f"Item {i}",
                    claims=claims,
                )
            )
        scan = _make_scan(items=items, items_found=20, items_scanned=20)
        db.save_scan(scan)

        loaded = db.load_scan(scan.scan_id)
        assert loaded is not None
        assert len(loaded.items) == 20
        for item in loaded.items:
            assert len(item.claims) == 5
            for claim in item.claims:
                assert len(claim.evidence) == 2


# ── Test: importability ──────────────────────────────────────────────────────


class TestImport:
    """Verify the public API is importable."""

    def test_import_from_store(self) -> None:
        """RepoGuardDB is importable from repoguard.store."""
        from repoguard.store import RepoGuardDB as DB

        assert DB is RepoGuardDB

    def test_import_from_database_module(self) -> None:
        """RepoGuardDB is importable from the database module."""
        from repoguard.store.database import RepoGuardDB as DB

        assert DB is RepoGuardDB
