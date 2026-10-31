"""SQLite-backed storage for RepoGuard scan results and configuration.

This module provides :class:`RepoGuardDB`, a thread-safe, context-managed
wrapper around an SQLite database that persists scan results, individual
items (issues/PRs), claims, evidence, and engine configuration.

Usage::

    from repoguard.store.database import RepoGuardDB

    with RepoGuardDB("repoguard.db") as db:
        db.save_scan(scan_result)
        scans = db.list_scans(repository="owner/repo")
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

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
    SCHEMA_VERSION,
)

# Internal DB schema version – bump when migrating tables.
_DB_SCHEMA_VERSION = 1

_CREATE_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id           TEXT PRIMARY KEY,
    schema_version    TEXT NOT NULL,
    repository        TEXT NOT NULL,
    scan_status       TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    scope             TEXT NOT NULL DEFAULT '',
    items_found       INTEGER NOT NULL DEFAULT 0,
    items_scanned     INTEGER NOT NULL DEFAULT 0,
    api_limitations   TEXT NOT NULL DEFAULT '[]',
    warnings          TEXT NOT NULL DEFAULT '[]',
    errors            TEXT NOT NULL DEFAULT '[]',
    ollama_used       INTEGER NOT NULL DEFAULT 0,
    sandbox_used      INTEGER NOT NULL DEFAULT 0,
    sandbox_available INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    rowid_pk              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id               TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    item_type             TEXT NOT NULL,
    number                INTEGER NOT NULL,
    title                 TEXT NOT NULL,
    author                TEXT NOT NULL,
    url                   TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT '',
    body                  TEXT NOT NULL DEFAULT '',
    assessment            TEXT NOT NULL,
    assessment_confidence TEXT NOT NULL,
    assessment_reason     TEXT NOT NULL DEFAULT '',
    ai_summary            TEXT,
    warnings              TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id        TEXT PRIMARY KEY,
    item_rowid      INTEGER NOT NULL REFERENCES items(rowid_pk) ON DELETE CASCADE,
    text            TEXT NOT NULL DEFAULT '',
    source_text     TEXT NOT NULL DEFAULT '',
    source_location TEXT,
    verdict         TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evidence (
    rowid_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    category      TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    file_path     TEXT,
    line_start    INTEGER,
    line_end      INTEGER,
    url           TEXT,
    snippet       TEXT,
    sandbox_log   TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scans_repository ON scans(repository);
CREATE INDEX IF NOT EXISTS idx_scans_started_at ON scans(started_at);
CREATE INDEX IF NOT EXISTS idx_items_scan_id ON items(scan_id);
CREATE INDEX IF NOT EXISTS idx_claims_item_rowid ON claims(item_rowid);
CREATE INDEX IF NOT EXISTS idx_evidence_claim_id ON evidence(claim_id);
"""


class RepoGuardDB:
    """Thread-safe SQLite storage for RepoGuard.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database file.  Use ``":memory:"``
        for a transient in-memory database (useful in tests).
    """

    def __init__(self, db_path: str = "repoguard.db") -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "RepoGuardDB":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ── Connection lifecycle ────────────────────────────────────────────

    def open(self) -> None:
        """Open the database connection and ensure the schema exists."""
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the active connection, raising if not open."""
        if self._conn is None:
            raise RuntimeError("Database is not open. Use open() or a context manager.")
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Yield a cursor inside a transaction that commits on success."""
        conn = self.connection
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # ── Schema migration ────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create or migrate the database schema."""
        conn = self.connection
        conn.executescript(_CREATE_TABLES_SQL)
        conn.commit()

        # Store schema version for future migrations.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("db_schema_version", str(_DB_SCHEMA_VERSION)),
        )
        conn.commit()

    # ── Scan CRUD ───────────────────────────────────────────────────────

    def save_scan(self, scan: ScanResult) -> None:
        """Persist a :class:`ScanResult` with all nested items, claims, and evidence.

        If a scan with the same ``scan_id`` already exists it is fully
        replaced (delete + re-insert).

        Parameters
        ----------
        scan:
            The scan result to store.
        """
        with self._transaction() as cur:
            # Delete existing scan (cascade handles children).
            cur.execute("DELETE FROM scans WHERE scan_id = ?", (scan.scan_id,))

            cur.execute(
                """INSERT INTO scans
                   (scan_id, schema_version, repository, scan_status,
                    started_at, completed_at, scope, items_found,
                    items_scanned, api_limitations, warnings, errors,
                    ollama_used, sandbox_used, sandbox_available)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan.scan_id,
                    scan.schema_version,
                    scan.repository,
                    scan.scan_status.value,
                    scan.started_at,
                    scan.completed_at,
                    scan.scope,
                    scan.items_found,
                    scan.items_scanned,
                    json.dumps(scan.api_limitations),
                    json.dumps(scan.warnings),
                    json.dumps(scan.errors),
                    int(scan.ollama_used),
                    int(scan.sandbox_used),
                    int(scan.sandbox_available),
                ),
            )

            for item in scan.items:
                self._insert_item(cur, scan.scan_id, item)

    def _insert_item(
        self, cur: sqlite3.Cursor, scan_id: str, item: ItemResult
    ) -> None:
        """Insert an item row and its child claims/evidence."""
        cur.execute(
            """INSERT INTO items
               (scan_id, item_type, number, title, author, url,
                created_at, body, assessment, assessment_confidence,
                assessment_reason, ai_summary, warnings)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan_id,
                item.item_type.value,
                item.number,
                item.title,
                item.author,
                item.url,
                item.created_at,
                item.body,
                item.assessment.value,
                item.assessment_confidence.value,
                item.assessment_reason,
                item.ai_summary,
                json.dumps(item.warnings),
            ),
        )
        item_rowid = cur.lastrowid

        for claim in item.claims:
            self._insert_claim(cur, item_rowid, claim)

    def _insert_claim(
        self, cur: sqlite3.Cursor, item_rowid: int, claim: Claim
    ) -> None:
        """Insert a claim row and its child evidence."""
        cur.execute(
            """INSERT INTO claims
               (claim_id, item_rowid, text, source_text, source_location,
                verdict, confidence, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.claim_id,
                item_rowid,
                claim.text,
                claim.source_text,
                claim.source_location,
                claim.verdict.value,
                claim.confidence.value,
                claim.reason,
            ),
        )

        for ev in claim.evidence:
            self._insert_evidence(cur, claim.claim_id, ev)

    @staticmethod
    def _insert_evidence(
        cur: sqlite3.Cursor, claim_id: str, ev: Evidence
    ) -> None:
        """Insert a single evidence row."""
        cur.execute(
            """INSERT INTO evidence
               (claim_id, evidence_type, category, description,
                file_path, line_start, line_end, url, snippet, sandbox_log)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id,
                ev.evidence_type.value,
                ev.category.value,
                ev.description,
                ev.file_path,
                ev.line_start,
                ev.line_end,
                ev.url,
                ev.snippet,
                ev.sandbox_log,
            ),
        )

    def load_scan(self, scan_id: str) -> Optional[ScanResult]:
        """Load a complete :class:`ScanResult` by its ID.

        Returns
        -------
        ScanResult or None
            The fully-hydrated scan result, or ``None`` if not found.
        """
        conn = self.connection
        row = conn.execute(
            "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_scan(conn, row)

    def get_scan(self, scan_id: str) -> Optional[ScanResult]:
        """Alias for load_scan."""
        return self.load_scan(scan_id)

    def _row_to_scan(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> ScanResult:
        """Reconstruct a ScanResult from a scans row + children."""
        scan_id = row["scan_id"]

        item_rows = conn.execute(
            "SELECT * FROM items WHERE scan_id = ? ORDER BY rowid_pk",
            (scan_id,),
        ).fetchall()

        items: List[ItemResult] = []
        for irow in item_rows:
            items.append(self._row_to_item(conn, irow))

        return ScanResult(
            scan_id=scan_id,
            schema_version=row["schema_version"],
            repository=row["repository"],
            scan_status=ScanStatus(row["scan_status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            scope=row["scope"],
            items_found=row["items_found"],
            items_scanned=row["items_scanned"],
            items=items,
            api_limitations=json.loads(row["api_limitations"]),
            warnings=json.loads(row["warnings"]),
            errors=json.loads(row["errors"]),
            ollama_used=bool(row["ollama_used"]),
            sandbox_used=bool(row["sandbox_used"]),
            sandbox_available=bool(row["sandbox_available"]),
        )

    def _row_to_item(
        self, conn: sqlite3.Connection, irow: sqlite3.Row
    ) -> ItemResult:
        """Reconstruct an ItemResult from an items row + children."""
        item_rowid = irow["rowid_pk"]
        claim_rows = conn.execute(
            "SELECT * FROM claims WHERE item_rowid = ? ORDER BY rowid",
            (item_rowid,),
        ).fetchall()

        claims: List[Claim] = []
        for crow in claim_rows:
            claims.append(self._row_to_claim(conn, crow))

        return ItemResult(
            item_type=ItemType(irow["item_type"]),
            number=irow["number"],
            title=irow["title"],
            author=irow["author"],
            url=irow["url"],
            created_at=irow["created_at"],
            body=irow["body"],
            claims=claims,
            assessment=OverallAssessment(irow["assessment"]),
            assessment_confidence=Confidence(irow["assessment_confidence"]),
            assessment_reason=irow["assessment_reason"],
            ai_summary=irow["ai_summary"],
            warnings=json.loads(irow["warnings"]),
        )

    @staticmethod
    def _row_to_claim(
        conn: sqlite3.Connection, crow: sqlite3.Row
    ) -> Claim:
        """Reconstruct a Claim from a claims row + child evidence."""
        claim_id = crow["claim_id"]
        ev_rows = conn.execute(
            "SELECT * FROM evidence WHERE claim_id = ? ORDER BY rowid_pk",
            (claim_id,),
        ).fetchall()

        evidence: List[Evidence] = []
        for erow in ev_rows:
            evidence.append(
                Evidence(
                    evidence_type=EvidenceType(erow["evidence_type"]),
                    category=FindingCategory(erow["category"]),
                    description=erow["description"],
                    file_path=erow["file_path"],
                    line_start=erow["line_start"],
                    line_end=erow["line_end"],
                    url=erow["url"],
                    snippet=erow["snippet"],
                    sandbox_log=erow["sandbox_log"],
                )
            )

        return Claim(
            claim_id=claim_id,
            text=crow["text"],
            source_text=crow["source_text"],
            source_location=crow["source_location"],
            verdict=ClaimVerdict(crow["verdict"]),
            confidence=Confidence(crow["confidence"]),
            reason=crow["reason"],
            evidence=evidence,
        )

    # ── Listing & filtering ─────────────────────────────────────────────

    def list_scans(
        self,
        *,
        repository: Optional[str] = None,
        status: Optional[ScanStatus] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ScanResult]:
        """List scans with optional filtering.

        Parameters
        ----------
        repository:
            Filter by repository (exact match, e.g. ``"owner/repo"``).
        status:
            Filter by scan status.
        since:
            ISO-8601 lower bound (inclusive) on ``started_at``.
        until:
            ISO-8601 upper bound (inclusive) on ``started_at``.
        limit:
            Maximum number of results (default 100).
        offset:
            Pagination offset.

        Returns
        -------
        list[ScanResult]
            Matching scans ordered by ``started_at`` descending.
        """
        conn = self.connection
        clauses: List[str] = []
        params: List[Any] = []

        if repository is not None:
            clauses.append("repository = ?")
            params.append(repository)
        if status is not None:
            clauses.append("scan_status = ?")
            params.append(status.value)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        sql = (
            f"SELECT * FROM scans {where} "
            f"ORDER BY started_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_scan(conn, r) for r in rows]

    # ── Delete ──────────────────────────────────────────────────────────

    def delete_scan(self, scan_id: str) -> bool:
        """Delete a scan and all its associated data.

        Returns
        -------
        bool
            ``True`` if a scan was deleted, ``False`` if no scan matched.
        """
        with self._transaction() as cur:
            cur.execute("DELETE FROM scans WHERE scan_id = ?", (scan_id,))
            return cur.rowcount > 0

    def clear_all(self) -> None:
        """Delete **all** scans and configuration from the database."""
        with self._transaction() as cur:
            cur.execute("DELETE FROM evidence")
            cur.execute("DELETE FROM claims")
            cur.execute("DELETE FROM items")
            cur.execute("DELETE FROM scans")
            cur.execute("DELETE FROM config")

    # ── Configuration ───────────────────────────────────────────────────

    _CONFIG_SENSITIVE_KEYS = frozenset({"github_token", "api_secret"})

    def get_config(self) -> EngineConfig:
        """Load engine configuration from the database.

        Returns a default :class:`EngineConfig` if nothing has been stored.
        Sensitive keys (``github_token``) are never read from the database.

        Returns
        -------
        EngineConfig
        """
        conn = self.connection
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        if not rows:
            return EngineConfig()

        raw: Dict[str, Any] = {}
        for row in rows:
            key = row["key"]
            if key in self._CONFIG_SENSITIVE_KEYS:
                continue
            try:
                raw[key] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                raw[key] = row["value"]

        return EngineConfig.from_dict(raw)

    def set_config(self, config: EngineConfig) -> None:
        """Persist engine configuration.

        The ``github_token`` field is **never** written to the database.

        Parameters
        ----------
        config:
            Engine configuration to persist.
        """
        data = config.to_dict()  # Already excludes github_token
        # Also explicitly strip sensitive keys for defense-in-depth.
        for key in self._CONFIG_SENSITIVE_KEYS:
            data.pop(key, None)

        with self._transaction() as cur:
            cur.execute("DELETE FROM config")
            for key, value in data.items():
                cur.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )

    def get_config_value(self, key: str) -> Optional[str]:
        """Retrieve a single configuration value by key.

        Parameters
        ----------
        key:
            Configuration key name.

        Returns
        -------
        str or None
            The raw string value, or ``None`` if the key is not set.
        """
        if key in self._CONFIG_SENSITIVE_KEYS:
            return None
        conn = self.connection
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_config_value(self, key: str, value: str) -> None:
        """Set a single configuration value.

        Parameters
        ----------
        key:
            Configuration key name.  Sensitive keys are rejected.
        value:
            The value to store (JSON-encoded recommended for non-strings).

        Raises
        ------
        ValueError
            If ``key`` is a sensitive key that must not be persisted.
        """
        if key in self._CONFIG_SENSITIVE_KEYS:
            raise ValueError(
                f"Refusing to store sensitive key {key!r} in the database."
            )
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
