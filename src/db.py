"""SQLite connection + versioned schema migrations.

Every connection runs any pending migrations first, so the schema upgrades
itself on the next run -- no separate migrate step to forget. The schema
version lives in SQLite's own header (PRAGMA user_version); each migration
runs in one write-locked transaction, so a crash mid-migration rolls back
cleanly and two processes can't migrate at the same time.

Kept to portable SQL where practical so a later move to Postgres (hosted,
multi-user, or sharing the WhatsApp agent's database) is a contained change.
"""
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline.sqlite"

# Used only by migration 2, to adopt pre-profile data into a "Default" profile.
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "product_profile.json"
DEFAULT_PROFILE_NAME = "Default"

BUSY_TIMEOUT_SECONDS = 15  # wait out a concurrent writer instead of failing immediately


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migration_1_initial_schema(conn: sqlite3.Connection) -> None:
    """The original Phase 1 schema. IF NOT EXISTS so databases created before
    migrations existed (user_version 0, tables already present) pass through.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            domain TEXT,
            employee_count INTEGER,
            industry TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            short_description TEXT,
            raw_json TEXT,
            icp_fit_score REAL,
            icp_fit_label TEXT,
            discovered_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            category TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            source_url TEXT,
            source_date TEXT,
            retrieved_at TEXT,
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique
            ON evidence_items (company_id, category, source_url)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            icp_fit_score REAL,
            icp_fit_label TEXT,
            signal_confidence TEXT,
            why_relevant_text TEXT,
            potential_use_case_text TEXT,
            evidence_ids_json TEXT,
            citation_issues TEXT,
            review_status TEXT DEFAULT 'pending',
            generated_at TEXT,
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )
        """
    )


def _has_legacy_data(conn: sqlite3.Connection) -> bool:
    return any(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        for table in ("companies", "evidence_items", "memos")
    )


def _migration_2_profiles(conn: sqlite3.Connection) -> None:
    """Multiple named profiles, with everything profile-dependent keyed by
    profile so profiles never overwrite each other's results:

    - profiles: the product + ICP config (formerly only product_profile.json)
    - runs: one row per pipeline run, with a snapshot of the config it used
    - company_scores: ICP score + breakdown per (profile, company); moved off
      companies, which now holds only profile-independent firmographics
    - evidence_items / memos: gain profile_id; memos gain run_id + review fields

    Existing rows are adopted into a "Default" profile loaded from
    config/product_profile.json. Evidence ids are preserved, since memo text
    cites them as [E<id>].
    """
    conn.execute(
        """
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id),
            config_snapshot_json TEXT NOT NULL,
            discover_limit INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            stats_json TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX idx_runs_profile ON runs (profile_id, id)")
    conn.execute(
        """
        CREATE TABLE company_scores (
            profile_id INTEGER NOT NULL REFERENCES profiles (id),
            company_id TEXT NOT NULL REFERENCES companies (id),
            score REAL NOT NULL,
            label TEXT NOT NULL,
            breakdown_json TEXT,
            scored_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, company_id)
        )
        """
    )

    default_profile_id = None
    if _has_legacy_data(conn):
        config = load_config(str(DEFAULT_PROFILE_PATH))
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO profiles (name, config_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (DEFAULT_PROFILE_NAME, json.dumps(config), now, now),
        )
        default_profile_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)
            SELECT ?, id, icp_fit_score, icp_fit_label, NULL, ?
            FROM companies WHERE icp_fit_score IS NOT NULL AND icp_fit_label IS NOT NULL
            """,
            (default_profile_id, now),
        )

    conn.execute("ALTER TABLE companies DROP COLUMN icp_fit_score")
    conn.execute("ALTER TABLE companies DROP COLUMN icp_fit_label")

    # SQLite can't add a NOT NULL foreign-key column in place: rebuild tables.
    conn.execute(
        """
        CREATE TABLE evidence_items_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id),
            company_id TEXT NOT NULL REFERENCES companies (id),
            category TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            source_url TEXT,
            source_date TEXT,
            retrieved_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO evidence_items_v2
            (id, profile_id, company_id, category, fact_text, source_url, source_date, retrieved_at)
        SELECT id, ?, company_id, category, fact_text, source_url, source_date, retrieved_at
        FROM evidence_items
        """,
        (default_profile_id,),
    )
    conn.execute("DROP TABLE evidence_items")
    conn.execute("ALTER TABLE evidence_items_v2 RENAME TO evidence_items")
    # Makes evidence retrieval safe to re-run: a retried company can't collect
    # duplicate rows, which the memo prompt would otherwise see twice.
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_evidence_unique
            ON evidence_items (profile_id, company_id, category, source_url)
        """
    )

    conn.execute(
        """
        CREATE TABLE memos_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id),
            company_id TEXT NOT NULL REFERENCES companies (id),
            run_id INTEGER REFERENCES runs (id),
            icp_fit_score REAL,
            icp_fit_label TEXT,
            signal_confidence TEXT,
            why_relevant_text TEXT,
            potential_use_case_text TEXT,
            evidence_ids_json TEXT,
            citation_issues TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending', 'needs_review', 'approved', 'rejected')),
            review_notes TEXT,
            reviewed_at TEXT,
            generated_at TEXT,
            UNIQUE (profile_id, company_id)
        )
        """
    )
    # Phase 1 only ever wrote one memo per company, but keep the newest if not.
    conn.execute(
        """
        INSERT INTO memos_v2
            (id, profile_id, company_id, run_id, icp_fit_score, icp_fit_label, signal_confidence,
             why_relevant_text, potential_use_case_text, evidence_ids_json, citation_issues,
             review_status, generated_at)
        SELECT id, ?, company_id, NULL, icp_fit_score, icp_fit_label, signal_confidence,
               why_relevant_text, potential_use_case_text, evidence_ids_json, citation_issues,
               COALESCE(review_status, 'pending'), generated_at
        FROM memos WHERE id IN (SELECT MAX(id) FROM memos GROUP BY company_id)
        """,
        (default_profile_id,),
    )
    conn.execute("DROP TABLE memos")
    conn.execute("ALTER TABLE memos_v2 RENAME TO memos")


# Append-only: never edit a migration that has shipped -- add a new one.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_1_initial_schema,
    _migration_2_profiles,
]


def _migrate(conn: sqlite3.Connection) -> None:
    while True:
        # IMMEDIATE takes the write lock up front, so a concurrent process
        # waits here and then re-reads the version instead of re-applying.
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= len(MIGRATIONS):
                conn.rollback()
                return
            MIGRATIONS[version](conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Migration {version + 1} left {len(violations)} foreign key violation(s)"
                )
            conn.execute(f"PRAGMA user_version = {version + 1}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        # WAL lets readers (the upcoming UI) proceed while a pipeline run writes.
        conn.execute("PRAGMA journal_mode = WAL")
        # Foreign keys stay off while migrating: table rebuilds would trip
        # them mid-way. _migrate verifies integrity before each commit.
        _migrate(conn)
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    return conn
